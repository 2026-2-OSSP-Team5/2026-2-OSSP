# 테스트

테스트 파일은 `test_*.py` 형식을 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest
```

환경·구문·코드 스타일 검사는 프로젝트 루트에서 다음 명령으로 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```
