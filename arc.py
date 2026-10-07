"""Project Arc: Spring Boot 소스의 실제 호출 흐름(조건 분기 포함)을 Tree로 출력한다.

사용: arc [프로젝트경로] [--all]    (--all: 외부 라이브러리 호출도 표시)
출력에는 파일명(경로 제외) / 변수 / 함수명만 나온다.
"""
import re
import sys
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Parser

PARSER = Parser(Language(tree_sitter_java.language()))
HTTP = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
        "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": "REQUEST"}
CLASS_TYPES = ("class_declaration", "interface_declaration", "record_declaration", "enum_declaration")
LOOPS = ("for_statement", "enhanced_for_statement", "while_statement", "synchronized_statement")
SWITCH = ("switch_expression", "switch_statement")
TRY = ("try_statement", "try_with_resources_statement")

CLS = {}  # ponytail: 단순 클래스명이 키 — 패키지만 다른 동명 클래스는 마지막 것이 이긴다
SUB = {}  # 상위 타입명 -> 직접 하위 클래스들
SHOW_ALL = False


class N:  # 흐름 Tree의 한 노드
    def __init__(self, label, kids=(), call=False, src=None):
        self.label, self.kids, self.call, self.src = label, list(kids), call, src


# ---------- 소스 인덱싱 ----------
def text(n):
    return n.text.decode() if n else ""


def flat(x):
    return " ".join((x if isinstance(x, str) else text(x)).split())


def cut(s, lim):
    return s if len(s) <= lim else s[:lim] + "…"


def tname(t):  # List<Foo> -> List, a.b.Foo -> Foo
    m = re.match(r"[\w.]+", text(t))
    return m.group().split(".")[-1] if m else None


def annotations(n):
    mods = next((c for c in n.children if c.type == "modifiers"), None)
    return [(text(a.child_by_field_name("name")).split(".")[-1], text(a))
            for a in (mods.children if mods else []) if a.type in ("annotation", "marker_annotation")]


def params(m):
    return [(p.child_by_field_name("type"), text(p.child_by_field_name("name")))
            for p in m.child_by_field_name("parameters").named_children if p.type == "formal_parameter"]


class Cls:
    def __init__(self, node, file):
        self.file = file
        self.name = text(node.child_by_field_name("name"))
        self.is_iface = node.type == "interface_declaration"
        self.anns = dict(annotations(node))
        self.supers = []
        for c in node.children:
            if c.type in ("superclass", "super_interfaces", "extends_interfaces"):
                for x in c.named_children:
                    self.supers += [tname(y) for y in (x.named_children if x.type == "type_list" else [x])]
        self.fields, self.methods = {}, {}
        body = node.child_by_field_name("body")
        for m in body.named_children if body else []:
            if m.type == "field_declaration":
                t = tname(m.child_by_field_name("type"))
                for d in m.named_children:
                    if d.type == "variable_declarator":
                        self.fields[text(d.child_by_field_name("name"))] = t
            elif m.type == "method_declaration":
                self.methods.setdefault(text(m.child_by_field_name("name")), []).append(m)


def load(root):
    CLS.clear(), SUB.clear()
    skipped = 0
    for f in sorted(root.rglob("*.java")):
        p = "/" + f.relative_to(root).as_posix()
        if any(x in p for x in ("/src/test/", "/build/", "/target/")):
            skipped += 1
            continue
        todo = [PARSER.parse(f.read_bytes()).root_node]
        while todo:
            n = todo.pop()
            if n.type in CLASS_TYPES:
                k = Cls(n, f.name)
                CLS[k.name] = k
            todo.extend(n.named_children)
    for k in CLS.values():
        for s in k.supers:
            SUB.setdefault(s, []).append(k)
    return skipped


# ---------- 타입/호출 대상 해석 ----------
def scope(k, m):  # 변수명 -> 타입명 (필드 + 파라미터 + 지역변수)
    sc = dict(k.fields)
    sc.update({n: tname(t) for t, n in params(m)})
    todo = [m.child_by_field_name("body")]
    while todo:
        n = todo.pop()
        if n.type == "local_variable_declaration":
            t = tname(n.child_by_field_name("type"))
            for d in n.named_children:
                if d.type == "variable_declarator":
                    sc[text(d.child_by_field_name("name"))] = (
                        type_of(d.child_by_field_name("value"), k, sc) if t == "var" else t)
        elif n.type == "enhanced_for_statement":
            sc[text(n.child_by_field_name("name"))] = tname(n.child_by_field_name("type"))
        todo.extend(reversed(n.children))
    return sc


def type_of(e, k, sc):
    if e is None:
        return None
    t = e.type
    if t == "identifier":
        n = text(e)
        return sc.get(n) or (n if n in CLS else None)
    if t == "this":
        return k.name
    if t == "field_access" and e.child_by_field_name("object").type == "this":
        return k.fields.get(text(e.child_by_field_name("field")))
    if t in ("object_creation_expression", "cast_expression"):
        return tname(e.child_by_field_name("type"))
    if t == "parenthesized_expression":
        return type_of(e.named_children[0], k, sc)
    if t == "method_invocation":
        r = recv(e, k, sc)
        ts = targets(r, text(e.child_by_field_name("name")), e.child_by_field_name("arguments").named_child_count)
        return tname(ts[0][1].child_by_field_name("type")) if ts else None


def recv(e, k, sc):
    obj = e.child_by_field_name("object")
    return k.name if obj is None else type_of(obj, k, sc)


def subs(name):
    return [x for k in SUB.get(name, []) for x in [k] + subs(k.name)]


def lookup(k, name):  # 상속 체인에서 메서드 찾기
    while k:
        if name in k.methods:
            return k.methods[name]
        k = next((CLS[s] for s in k.supers if s in CLS), None)
    return []


def targets(cname, name, argc):  # 인터페이스면 구현체까지 후보로
    out, seen = [], set()
    for k in ([CLS[cname]] + subs(cname)) if cname in CLS else []:
        ms = lookup(k, name)
        ms = [m for m in ms if len(m.child_by_field_name("parameters").named_children) == argc] or ms
        if ms and ms[0].id not in seen:
            seen.add(ms[0].id)
            out.append((k, ms[0]))
    return [x for x in out if x[1].child_by_field_name("body")] or out[:1]


# ---------- 흐름 추출 ----------
def expand(k, m, st):
    body = m.child_by_field_name("body")
    if m.id in st:
        return [N("↻ 재귀 호출")]
    return w(body, (k, scope(k, m), st | {m.id})) if body else []


def kids(n, c, skip=()):
    return [x for ch in n.named_children if ch not in skip for x in w(ch, c)]


def head(n, body):  # 본문 직전까지의 헤더 텍스트: for (...), catch (...) 등
    return cut(flat(n.text[: body.start_byte - n.start_byte].decode()), 100)


def tag(e, r, prefix):  # 'dto = ', 'return ' 접두를 e가 만든 호출 노드에 붙인다
    for x in r:
        if x.src == e.id:
            x.label = prefix + x.label
    return any(x.src == e.id for x in r)


def if_(n, c, kw):
    cond = n.child_by_field_name("condition")
    out = w(cond, c) + [N(f"{kw} {cut(flat(cond), 100)}", w(n.child_by_field_name("consequence"), c))]
    alt = n.child_by_field_name("alternative")
    if alt is not None:
        if alt.type == "if_statement":
            out += if_(alt, c, "else if")
        else:
            out.append(N("else", w(alt, c)))
    return out


def w(n, c):
    """AST 노드 -> 흐름 N 리스트. c = (현재 클래스, 변수 스코프, 재귀 방지 집합)"""
    if n is None:
        return []
    t, (k, sc, st) = n.type, c
    if t == "if_statement":
        return if_(n, c, "if")
    if t in LOOPS:
        body = n.child_by_field_name("body")
        return kids(n, c, [body]) + [N(head(n, body), w(body, c))]
    if t == "do_statement":
        cond = n.child_by_field_name("condition")
        return [N("do-while " + cut(flat(cond), 100), w(n.child_by_field_name("body"), c))] + w(cond, c)
    if t in SWITCH:
        cond, cases = n.child_by_field_name("condition"), []
        for g in n.child_by_field_name("body").named_children:
            labels = [re.sub(r"\s*(:|->)$", "", flat(x)) for x in g.children if x.type == "switch_label"]
            rest = [x for x in g.named_children if x.type != "switch_label"]
            cases.append(N("; ".join(labels), [y for s in rest for y in w(s, c)]))
        return w(cond, c) + [N("switch " + cut(flat(cond), 100), cases)]
    if t in TRY:
        out = w(n.child_by_field_name("resources"), c) + [N("try", w(n.child_by_field_name("body"), c))]
        for ch in n.named_children:
            if ch.type == "catch_clause":
                b = ch.child_by_field_name("body")
                out.append(N(head(ch, b), w(b, c)))
            elif ch.type == "finally_clause":
                out.append(N("finally", kids(ch, c)))
        return out
    if t == "ternary_expression":
        cond, a, b = (n.child_by_field_name(x) for x in ("condition", "consequence", "alternative"))
        ka, kb = w(a, c), w(b, c)
        return w(cond, c) + ([N("? " + cut(flat(cond), 100), [N("true", ka), N("false", kb)])] if ka or kb else [])
    if t in ("return_statement", "throw_statement"):
        e = n.named_children[0] if n.named_children else None
        r, word = w(e, c), t.split("_")[0]
        if word == "return" and e is not None and tag(e, r, "return "):
            return r
        return r + [N(f"{word} {cut(flat(e), 60)}".strip())]
    if t == "variable_declarator":
        v = n.child_by_field_name("value")
        r = w(v, c)
        if v is not None:
            tag(v, r, text(n.child_by_field_name("name")) + " = ")
        return r
    if t == "assignment_expression":
        left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
        r = w(left, c) + w(right, c)
        tag(right, r, flat(left) + " = ")
        return r
    if t == "method_invocation":
        obj, args = n.child_by_field_name("object"), n.child_by_field_name("arguments")
        name = text(n.child_by_field_name("name"))
        who = "" if obj is None else flat(obj) if obj.type in ("identifier", "field_access", "this") else "…"
        label = f"{who + '.' if who else ''}{name}({cut(flat(args)[1:-1], 40)})"
        r = recv(n, k, sc)
        ts = targets(r, name, args.named_child_count)
        if ts:
            nodes = [N(f"{label}  [{k2.file}]", expand(k2, m2, st), True, n.id) for k2, m2 in ts]
        elif r in CLS and CLS[r].is_iface:  # JpaRepository 등 본문 없는 인터페이스
            nodes = [N(f"{label}  [{CLS[r].file}]", [], True, n.id)]
        elif SHOW_ALL:
            nodes = [N(label, [], True, n.id)]
        else:
            nodes = []
        return w(obj, c) + w(args, c) + nodes
    if t == "object_creation_expression":
        tn, args = tname(n.child_by_field_name("type")), n.child_by_field_name("arguments")
        label = f"new {tn}({cut(flat(args)[1:-1], 40)})"
        if tn in CLS:
            return kids(n, c) + [N(f"{label}  [{CLS[tn].file}]", [], True, n.id)]
        return kids(n, c) + ([N(label, [], True, n.id)] if SHOW_ALL else [])
    return kids(n, c)


# ---------- 출력 ----------
def lines(nodes, pre=""):
    out = []
    for i, n in enumerate(nodes):
        last = i == len(nodes) - 1
        out.append(pre + ("└─ " if last else "├─ ") + n.label)
        out += lines(n.kids, pre + ("   " if last else "│  "))
    return out


def sig(m):  # 프로젝트 클래스 타입은 파일명으로 표기: EqpDto -> EqpDto.java
    show = lambda t: re.sub(r"\b[A-Z]\w*", lambda x: CLS[x.group()].file if x.group() in CLS else x.group(), flat(t))
    return ", ".join(f"{show(t)} {n}" for t, n in params(m))


def path_of(ann):
    return (re.findall(r'"([^"]*)"', ann or "") or [""])[0]


def arc(root):
    skipped = load(root)
    out, seen = [], set()
    for k in CLS.values():
        base, eps = path_of(k.anns.get("RequestMapping")), []
        for ms in k.methods.values():
            for m in ms:
                anns = dict(annotations(m))
                verb = next((HTTP[a] for a in anns if a in HTTP), None)
                if not verb and "Operation" not in anns:  # API 시작점 = 매핑 또는 @Operation 메서드
                    continue
                path = next((path_of(t) for a, t in anns.items() if a in HTTP), "")
                label = f"{verb + ' ' if verb else ''}{base}{path}  {text(m.child_by_field_name('name'))}({sig(m)})"
                if label in seen:  # 인터페이스와 구현체에 같은 선언이 중복될 때
                    continue
                seen.add(label)
                # API 인터페이스처럼 본문이 없으면 구현체 메서드로 따라간다
                ts = [(k, m)] if m.child_by_field_name("body") else targets(k.name, text(m.child_by_field_name("name")), len(params(m)))
                if ts and ts[0][0] is not k:
                    label += f"  [{ts[0][0].file}]"
                eps.append(N(label.strip(), [x for k2, m2 in ts for x in expand(k2, m2, frozenset())]))
        if eps:
            out += [k.file] + lines(eps) + [""]
    return "\n".join(out) or f"API 시작점(@*Mapping/@Operation 메서드)을 찾지 못했습니다. (검색 경로: {root} / 클래스 {len(CLS)}개 / 제외된 .java {skipped}개)"


def main():
    sys.setrecursionlimit(10000)
    sys.stdout.reconfigure(encoding="utf-8"), sys.stderr.reconfigure(encoding="utf-8")
    global SHOW_ALL
    a = sys.argv[1:]
    SHOW_ALL = "--all" in a
    out = arc(Path(next((x for x in a if not x.startswith("--")), ".")).resolve())
    print(out)
    Path("PA.txt").write_text(out, encoding="utf-8")  # 실행한 폴더에 저장
    print("-> PA.txt 저장 완료", file=sys.stderr)


if __name__ == "__main__":
    main()
