import tempfile
from pathlib import Path

import arc

FILES = {
    "src/main/java/a/EqpController.java": """
@RestController @RequestMapping("/eqp")
public class EqpController {
  private final EqpService eqpService;
  @GetMapping("/{id}")
  public EqpDto get(@PathVariable Long id) {
    if (id == null) { throw new IllegalArgumentException("x"); }
    EqpDto dto = eqpService.find(id);
    for (Long x : eqpService.ids()) { eqpService.touch(x); }
    switch (dto.getType()) { case "A": eqpService.touch(1L); break; default: break; }
    try { return eqpService.find(id); } catch (Exception e) { log.error("x"); } finally { eqpService.touch(0L); }
    return dto;
  }
}""",
    "src/main/java/a/EqpApi.java": """
@RequestMapping("/v2")
public interface EqpApi {
  @Operation(summary = "x") @GetMapping("/eqp/{id}")
  EqpDto getEqp(@PathVariable Long id);
}""",
    "src/main/java/a/EqpApiController.java": """
@RestController
public class EqpApiController implements EqpApi {
  private final EqpService eqpService;
  public EqpDto getEqp(Long id) { return eqpService.find(id); }
}""",
    "src/main/java/a/EqpService.java": "interface EqpService { EqpDto find(Long id); java.util.List<Long> ids(); void touch(Long id); }",
    "src/main/java/a/EqpServiceImpl.java": """
@Service class EqpServiceImpl implements EqpService {
  private final EqpRepository eqpRepository;
  public EqpDto find(Long id) {
    if (id < 0) { return null; }
    else if (id == 0) { return new EqpDto(); }
    else { return eqpRepository.findById(id).orElseThrow(); }
  }
  public java.util.List<Long> ids() { return eqpRepository.findAllIds(); }
  public void touch(Long id) { eqpRepository.save(null); }
}""",
    "src/main/java/a/EqpRepository.java": "interface EqpRepository extends JpaRepository<EqpDto, Long> { java.util.List<Long> findAllIds(); }",
    "src/main/java/a/EqpDto.java": "class EqpDto { String getType() { return null; } }",
}


def test_arc():
    with tempfile.TemporaryDirectory() as d:
        for p, src in FILES.items():
            (Path(d) / p).parent.mkdir(parents=True, exist_ok=True)
            (Path(d) / p).write_text(src)
        out = arc.arc(Path(d))
    print(out)
    for want in ["EqpController.java", "GET /eqp/{id}  get(Long id)", "if (id == null)", "throw new IllegalArgumentException",
                 "dto = eqpService.find(id)  [EqpServiceImpl.java]", "else if (id == 0)", "else",
                 "return new EqpDto()  [EqpDto.java]", "eqpRepository.findById(id)  [EqpRepository.java]",
                 "for (Long x : eqpService.ids())", 'case "A"', "default", "catch (Exception e)", "finally",
                 "EqpApi.java", "GET /v2/eqp/{id}  getEqp(Long id)  [EqpApiController.java]"]:
        assert want in out, want
    assert "return eqpService.find(id)  [EqpServiceImpl.java]" in out.split("EqpController.java")[0]  # API 인터페이스 -> 구현체 추적
    assert "/src/" not in out and "log.error" not in out  # 경로/외부호출은 숨김


if __name__ == "__main__":
    test_arc()
    print("OK")
