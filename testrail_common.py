"""Shared TestRail API and backup helpers. Python 3.10+."""
import hashlib, json, os, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests

class API:
    def __init__(self, base, user, key):
        self.base = base.rstrip('/')
        self.api = self.base + '/index.php?/api/v2/'
        self.s = requests.Session()
        self.s.auth = (user, key)
        self.s.headers.update({'Accept': 'application/json'})

    def request(self, method, endpoint, data=None, *, binary=False):
        url = self.api + endpoint.lstrip('/')
        for attempt in range(5):
            try:
                kw = {'timeout': (20, 180)}
                if data is not None:
                    if isinstance(data, dict) and '__file__' in data:
                        with open(data['__file__'], 'rb') as f:
                            response = self.s.post(url, files={'attachment': f}, timeout=(20, 600))
                    else:
                        response = self.s.request(method, url, json=data, **kw)
                else:
                    response = self.s.request(method, url, **kw)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                    retry_after = response.headers.get('Retry-After', '')
                    delay = min(120, float(retry_after)) if retry_after.replace('.', '', 1).isdigit() else min(60, 2 ** (attempt + 1))
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                if binary:
                    return response.content
                if not response.content:
                    return {}
                return response.json()
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 4: raise
                time.sleep(2 ** attempt)
        raise RuntimeError('API request failed: ' + endpoint)

    def get(self, endpoint): return self.request('GET', endpoint)
    def post(self, endpoint, data): return self.request('POST', endpoint, data)

    def collection(self, endpoint, key):
        """Handle both legacy list and paginated TestRail API formats."""
        output, offset = [], 0
        while True:
            separator = '&' if '?' in endpoint else '&'  # TestRail uses & after /api/v2/path
            page = self.get(f'{endpoint}{separator}limit=250&offset={offset}')
            if isinstance(page, list):
                output.extend(page)
                break
            if not isinstance(page, dict):
                raise ValueError('Unexpected API response for ' + endpoint)
            chunk = page.get(key)
            if chunk is None:
                raise ValueError(f'Missing {key} in response from {endpoint}: {list(page)}')
            output.extend(chunk)
            links = page.get('_links') or {}
            if not links.get('next') or not chunk:
                break
            offset += len(chunk)
        return output

    def download_attachment(self, attachment_id):
        return self.request('GET', f'get_attachment/{attachment_id}', binary=True)

    def download_embedded(self, url):
        """Download same-host media; reject login pages, external redirects and HTML."""
        parsed = urlparse(urljoin(self.base + '/', url))
        home = urlparse(self.base)
        if parsed.scheme != home.scheme or parsed.netloc.lower() != home.netloc.lower():
            raise ValueError('Refusing external or insecure media URL')
        current = parsed.geturl()
        for _ in range(5):
            for attempt in range(5):
                r = self.s.get(current, timeout=(20, 180), allow_redirects=False)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                    retry_after = r.headers.get('Retry-After', '')
                    delay = min(120, float(retry_after)) if retry_after.replace('.', '', 1).isdigit() else min(60, 2 ** (attempt + 1))
                    time.sleep(delay)
                    continue
                break
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get('Location')
                if not location: raise ValueError('Redirect without Location')
                next_url = urljoin(current, location)
                target = urlparse(next_url)
                if target.scheme != home.scheme or target.netloc.lower() != home.netloc.lower():
                    raise ValueError('Embedded media redirects to external host')
                current = next_url
                continue
            r.raise_for_status()
            content_type = r.headers.get('Content-Type', '').lower()
            if 'text/html' in content_type or r.content[:100].lstrip().lower().startswith((b'<!doctype html', b'<html')):
                raise ValueError('TestRail returned an HTML page instead of attachment bytes (browser session may be required)')
            return r.content
        raise ValueError('Too many embedded media redirects')


def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding='utf-8')


def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8'))

def checksum(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()

def safe_filename(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s))
    return s.strip(' .')[:140] or 'unnamed'

def file_record(path, root):
    p = Path(path)
    return {'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': checksum(p)}

def require_env(prefix):
    from dotenv import load_dotenv
    load_dotenv()

    if prefix == "":
        base = os.getenv("TESTRAIL_BASE_URL", "").strip()
        user = os.getenv("TESTRAIL_EMAIL", "").strip()
        key = os.getenv("TESTRAIL_PASSWORD", "").strip()
    else:
        base = os.getenv(f"{prefix}TESTRAIL_URL", "").strip()
        user = os.getenv(f"{prefix}TESTRAIL_USER", "").strip()
        key = os.getenv(f"{prefix}TESTRAIL_API_KEY", "").strip()

    if not all([base, user, key]):
        raise SystemExit("Missing TestRail credentials in .env")

    return API(base, user, key)

# Matches src="..." or src='...' in rich text; retain original formatting for restore.
IMAGE_RE = re.compile(r'(?P<prefix>\bsrc\s*=\s*["\'])(?P<url>[^"\']+)(?P<suffix>["\'])', re.I)
