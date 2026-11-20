"""Versioned in-memory server: detached reads and atomic conditional merges."""
import copy
import threading
from test_runtime import NetBoxFake
from runtime import APIError


class VersionedNetBox(NetBoxFake):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.version = 1
        self.attempted = []
        self.committed = []
        self.objects = {}
        self.device.update(site={'id': 2}, location=None, rack=None, cluster=None)

    def get(self, path):
        return self.request('GET', path)[0]

    def request(self, method, path, payload=None, headers=None):
        with self.lock:
            self.attempted.append((method, path, copy.deepcopy(payload), copy.deepcopy(headers)))
            if path == 'api/dcim/devices/42/':
                if method == 'GET':
                    return copy.deepcopy(self.device), {'ETag': 'W/"%s"' % self.version}
                if method == 'PATCH':
                    if (headers or {}).get('If-Match') != 'W/"%s"' % self.version:
                        raise APIError(412)
                    for field, value in payload.items():
                        if field == 'custom_fields':
                            self.device[field].update(copy.deepcopy(value))
                        else:
                            self.device[field] = {'id': value}
                    self.version += 1
                    self.committed.append(copy.deepcopy(payload))
                    return copy.deepcopy(self.device), {}
            if method == 'GET' and path in self.objects:
                return copy.deepcopy(self.objects[path]), {}
            return super().request(method, path, payload, headers)
