"""QA 화면용 접속 설정(qa/config.js)을 만든다.

- Supabase CLI(로그인된 상태)로 프로젝트의 공개 키(새 publishable 키, 없으면 옛 anon 키)만 가져온다. 서비스 키·DB 비밀번호는 쓰지 않는다.
- 만들어진 config.js 는 .gitignore 대상이다(공개 키라도 저장소에 올리지 않는다).

실행: python qa/make_config.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_REF_FILE = ROOT / "supabase" / ".temp" / "project-ref"


def pick_public_key(keys):
    """새 publishable 키가 있으면 그것을, 없으면 옛 anon 키를 고른다(키 값은 출력하지 않는다)."""
    new = next((k["api_key"] for k in keys if str(k.get("api_key", "")).startswith("sb_publishable_")), None)
    return new or next((k["api_key"] for k in keys if k.get("name") == "anon"), None)


def main():
    if not PROJECT_REF_FILE.exists():
        sys.exit("프로젝트가 연결되어 있지 않습니다. 먼저 npx supabase link --project-ref <ref> 를 실행하세요.")
    ref = PROJECT_REF_FILE.read_text(encoding="utf-8").strip()
    r = subprocess.run(["npx", "--yes", "supabase", "projects", "api-keys", "--project-ref", ref, "-o", "json"],
                       capture_output=True, text=True, shell=(sys.platform == "win32"), cwd=ROOT)
    if r.returncode != 0:
        sys.exit("키를 가져오지 못했습니다. npx supabase login 이 되어 있는지 확인하세요.\n" + r.stderr[-300:])
    keys = json.loads(r.stdout)
    key = pick_public_key(keys)
    if not key:
        sys.exit("공개 키(publishable 또는 anon)를 찾지 못했습니다.")
    body = "window.QA_CONFIG = " + json.dumps({"url": f"https://{ref}.supabase.co", "publishableKey": key}, indent=2) + ";\n"
    (ROOT / "qa" / "config.js").write_text(body, encoding="utf-8", newline="\n")
    kind = "publishable" if key.startswith("sb_publishable_") else "옛 anon"
    print(f"만들었습니다: qa/config.js (공개 키만 포함, {kind} 키)")


if __name__ == "__main__":
    main()
