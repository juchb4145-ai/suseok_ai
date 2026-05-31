from __future__ import annotations

from pathlib import Path


class LocalNaverResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None


class LocalNaverFixtureSession:
    def __init__(self, list_html: str | Path, detail_dir: str | Path) -> None:
        self.list_html = Path(list_html)
        self.detail_dir = Path(detail_dir)

    def get(self, url: str, **_) -> LocalNaverResponse:
        if "sise_group_detail.naver" in str(url):
            detail_no = str(url).split("no=", 1)[1].split("&", 1)[0]
            path = self.detail_dir / f"detail_{detail_no}.html"
        else:
            path = self.list_html
        return LocalNaverResponse(path.read_text(encoding="utf-8"))
