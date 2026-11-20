# Modified for portable extraction: installation-specific examples use fictional identities.
"""Shared configuration, strict HTTPS transport and explicit Device identity."""
import hashlib
import json
import signal
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class InventoryError(Exception):
    """Safe operational message; never includes upstream bodies or credentials."""


def install_shutdown_handler():
    """Honor Kubernetes termination even when Python is the container's PID 1."""
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)


class APIError(InventoryError):
    def __init__(self, status=0):
        self.status = status
        super().__init__('API request failed (status %s)' % status)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError(code)


class HTTPSClient:
    def __init__(self, base, token_file, ca_file=None, bearer=False, opener=None, sleep=time.sleep):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InventoryError('HTTPS URL without credentials/query/fragment required')
        self.base = base.rstrip('/') + '/'
        self.origin = (parsed.scheme, parsed.netloc)
        self.token_file, self.bearer, self.sleep = token_file, bearer, sleep
        self.opener = opener or urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))

    def url(self, path):
        url = urllib.parse.urljoin(self.base, path)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc) != self.origin or parsed.username or parsed.password or parsed.fragment:
            raise InventoryError('refused unexpected API origin')
        # NetBox URL may contain a deployment prefix. Never leave that prefix.
        if not parsed.path.startswith(urllib.parse.urlsplit(self.base).path):
            raise InventoryError('refused unexpected API path')
        return url

    def request(self, method, path, payload=None, headers=None, lines=False, timeout=30):
        url = self.url(path)
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                with open(self.token_file, encoding='utf-8') as stream:
                    token = stream.read().strip()
                if not token or any(c.isspace() for c in token):
                    raise InventoryError('invalid token file')
                scheme = 'Bearer' if self.bearer or token.startswith('nbt_') else 'Token'
                request_headers = {'Authorization': scheme + ' ' + token, 'Accept': 'application/json',
                                   'User-Agent': 'netbox-node-inventory/0.2.0'}
                if payload is not None:
                    request_headers['Content-Type'] = 'application/json'
                request_headers.update(headers or {})
                request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                                 headers=request_headers, method=method)
                with self.opener.open(request, timeout=timeout) as response:
                    if response.status >= 300:
                        raise APIError(response.status)
                    data = response.read(16 * 1024 * 1024 + 1)
                    if len(data) > 16 * 1024 * 1024:
                        raise InventoryError('API response too large')
                    return ([json.loads(line) for line in data.splitlines() if line.strip()] if lines else json.loads(data)), response.headers
            except urllib.error.HTTPError as error:
                failure = APIError(error.code)
            except (urllib.error.URLError, OSError, ValueError):
                failure = APIError()
            except APIError as error:
                failure = error
            if failure.status not in (0, 429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise failure from None
            self.sleep(2 ** attempt)

    def get(self, path):
        return self.request('GET', path)[0]

    def listing(self, path, filters=None):
        path += ('?' + urllib.parse.urlencode(filters)) if filters else ''
        rows, seen = [], set()
        while path:
            url = self.url(path)
            if url in seen:
                raise InventoryError('API pagination cycle')
            seen.add(url)
            page = self.get(url)
            if not isinstance(page, dict) or not isinstance(page.get('results'), list):
                raise InventoryError('incomplete API list')
            rows.extend(page['results'])
            path = page.get('next')
            if not path and page.get('count') != len(rows):
                raise InventoryError('incomplete API list')
        return rows


def load_config(path):
    try:
        with open(path, encoding='utf-8') as stream:
            config = json.load(stream)
    except (OSError, ValueError):
        raise InventoryError('unable to read configuration') from None
    if not isinstance(config, dict) or not isinstance(config.get('cluster_id'), str) or not config['cluster_id'].strip():
        raise InventoryError('cluster_id is required')
    defaults = {'netbox_url': 'https://netbox.example.com', 'dry_run': True, 'device_mappings': {},
                'token_file': '/run/netbox/token', 'host_root': '/host', 'interval_seconds': 3600,
                'jitter_seconds': 60, 'grace_period_seconds': 300, 'relist_seconds': 300, 'watch_timeout_seconds': 60}
    config = {**defaults, **config}
    if not isinstance(config['dry_run'], bool) or not isinstance(config['device_mappings'], dict):
        raise InventoryError('invalid dry_run or device_mappings')
    ids = []
    for node, mapping in config['device_mappings'].items():
        if not isinstance(node, str) or not isinstance(mapping, dict):
            raise InventoryError('invalid Device mapping')
        identity = mapping.get('device_id')
        if type(identity) is not int or identity <= 0:
            raise InventoryError('positive mapped Device ID required')
        if mapping.get('expected_serial') is not None and not isinstance(mapping['expected_serial'], str):
            raise InventoryError('invalid expected_serial')
        if mapping.get('serial_source', 'product_serial') not in ('product_serial', 'board_serial'):
            raise InventoryError('invalid serial_source')
        ids.append(identity)
    if len(set(ids)) != len(ids):
        raise InventoryError('ambiguous duplicate Device mapping')
    for key in ('interval_seconds', 'relist_seconds', 'watch_timeout_seconds', 'grace_period_seconds', 'jitter_seconds'):
        if type(config[key]) is not int or config[key] < (1 if key in ('interval_seconds', 'relist_seconds', 'watch_timeout_seconds') else 0):
            raise InventoryError('invalid interval configuration')
    from placement import validate_placement
    config['placement'] = validate_placement(config.get('placement', {}), config['device_mappings'])
    return config


def marker(config, node):
    return 'node-inventory:' + hashlib.sha256((config['cluster_id'] + '/' + node).encode()).hexdigest()[:24]


def owner_check(value, config, node):
    if value is not None and (not isinstance(value, dict) or value.get('cluster_id') != config['cluster_id'] or value.get('node_name') != node):
        raise InventoryError('refused conflicting custom field owner')


def update_field(client, device_id, key, value, config, node, validate=None):
    """PATCH only our field; ETag prevents concurrent custom-field merge lost updates."""
    path = 'api/dcim/devices/%s/' % device_id
    for attempt in range(3):
        device, headers = client.request('GET', path)
        fields = device.get('custom_fields', {})
        if key not in fields:
            raise InventoryError('required pre-provisioned JSON custom field missing')
        owner_check(fields[key], config, node)
        if validate:
            validate(device)
        if fields[key] == value:
            return
        etag = headers.get('ETag')
        if not etag:
            raise InventoryError('NetBox ETag required for safe update')
        try:
            client.request('PATCH', path, {'custom_fields': {key: value}}, {'If-Match': etag})
            return
        except APIError as error:
            if error.status != 412 or attempt == 2:
                raise
