import json
import re
from urllib.parse import urlparse, parse_qs, unquote

class GeminiHarAnalysis:
    def __init__(self):
        self.base_url = "https://gemini.google.com"
        self.chat_endpoint = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
        self.headers = {}
        self.cookies = ""
        self.bl_param = ""
        self.f_sid = ""
        self.hl = "zh-CN"
        self._reqid = 4569005
        self.rt = "c"
        self.at = ""
        self.sn_param = "M2UyNjgxMmMtZjljOS00"
        self.template_params = {}
        self.content_field_path = ""
        self.is_streaming = False

def parse_har(har_path):
    with open(har_path, "r", encoding="utf-8") as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", [])
    analysis = GeminiHarAnalysis()

    chat_entry = None
    for entry in entries:
        req = entry.get("request", {})
        url = req.get("url", "")
        if "StreamGenerate" in url:
            chat_entry = entry
            break

    if not chat_entry:
        chat_entry = entries[0]

    req = chat_entry.get("request", {})
    url = req.get("url", "")
    parsed = urlparse(url)

    analysis.headers = {}
    for h in req.get("headers", []):
        name = h.get("name", "")
        val = h.get("value", "")
        skip = {":method", ":path", ":authority", ":scheme", "content-length", "cookie"}
        if name.lower() not in skip:
            analysis.headers[name] = val

    for h in req.get("headers", []):
        if h.get("name", "").lower() == "cookie":
            analysis.cookies = h.get("value", "")

    qs = parse_qs(parsed.query)
    analysis.bl_param = qs.get("bl", [""])[0]
    analysis.f_sid = qs.get("f.sid", [""])[0]
    analysis.hl = qs.get("hl", ["zh-CN"])[0]
    analysis._reqid = int(qs.get("_reqid", [4569005])[0])
    analysis.rt = qs.get("rt", ["c"])[0]

    post_data = req.get("postData", {})
    text = post_data.get("text", "")
    if "f.req=" in text:
        parsed_body = parse_qs(text)
        freq = parsed_body.get("f.req", [""])[0]
        decoded = unquote(freq)
        try:
            data = json.loads(decoded)
            if isinstance(data, list) and len(data) >= 2:
                inner = json.loads(data[1])
                if isinstance(inner, list) and len(inner) >= 4:
                    analysis.template_params = {
                        "user_message": inner[0][0] if isinstance(inner[0], list) and len(inner[0]) > 0 else "",
                        "conversation_id": inner[0][1] if isinstance(inner[0], list) and len(inner[0]) > 1 else 0,
                        "language": inner[1][0] if isinstance(inner[1], list) and len(inner[1]) > 0 else "zh-CN",
                        "token": inner[3] if len(inner) > 3 else "",
                    }
        except:
            pass

        if "&at=" in text:
            at_match = re.search(r"&at=([^&]+)", text)
            if at_match:
                analysis.at = at_match.group(1)

        if "&_reqid=" in text:
            reqid_match = re.search(r"&_reqid=(\d+)", url) or re.search(r"&_reqid=(\d+)", text)
            if reqid_match:
                analysis._reqid = int(reqid_match.group(1))

    resp = chat_entry.get("response", {})
    resp_text = resp.get("content", {}).get("text", "")
    if resp_text:
        analysis.content_field_path = _find_content_path(resp_text)

    seen = set()
    for entry in entries:
        ep = urlparse(entry.get("request", {}).get("url", "")).path
        if ep and ep not in seen:
            seen.add(ep)
    analysis.all_endpoints = list(seen)

    return analysis

def _find_content_path(resp_text):
    lines = resp_text.strip().split("\n")
    for i, line in enumerate(lines):
        if i % 2 == 1 and line.strip():
            try:
                data = json.loads(line.strip())
                if isinstance(data, list) and len(data) > 0:
                    inner = data[0]
                    if isinstance(inner, list) and len(inner) >= 3:
                        third = inner[2]
                        if isinstance(third, str):
                            try:
                                third_data = json.loads(third)
                                if isinstance(third_data, list):
                                    fourth = third_data[3]
                                    if isinstance(fourth, list) and len(fourth) > 0:
                                        fifth = fourth[0]
                                        if isinstance(fifth, list) and len(fifth) >= 2:
                                            content = fifth[1]
                                            if isinstance(content, list) and len(content) > 0:
                                                return "batch_response.content"
                            except:
                                pass
            except:
                pass
    return "batch_response.content"

if __name__ == "__main__":
    analysis = parse_har("gemini.google.com.har")
    print(f"Base URL: {analysis.base_url}")
    print(f"Chat Endpoint: {analysis.chat_endpoint}")
    print(f"Content Field: {analysis.content_field_path}")
    print(f"Template Params: {analysis.template_params}")
    print(f"Headers count: {len(analysis.headers)}")
