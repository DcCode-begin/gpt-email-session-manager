import re
from html.parser import HTMLParser


KEYWORDS = (
    "验证码",
    "校验码",
    "动态码",
    "确认码",
    "安全码",
    "verification",
    "verify",
    "security code",
    "one-time",
    "otp",
    "code",
)

NUMERIC_CODE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
ALNUM_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{4,8})(?![A-Za-z0-9])")


class HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self.parts)


def html_to_text(html: str) -> str:
    parser = HtmlTextExtractor()
    parser.feed(html)
    return parser.text()


def extract_verification_code(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text or "")
    lowered = normalized.lower()

    for keyword in KEYWORDS:
        start = 0
        while True:
            index = lowered.find(keyword.lower(), start)
            if index == -1:
                break
            window = normalized[max(0, index - 80) : index + 160]
            match = NUMERIC_CODE.search(window)
            if match:
                return match.group(1)
            match = ALNUM_CODE.search(window)
            if match and any(char.isdigit() for char in match.group(1)):
                return match.group(1)
            start = index + len(keyword)

    match = NUMERIC_CODE.search(normalized)
    if match:
        return match.group(1)

    return None

