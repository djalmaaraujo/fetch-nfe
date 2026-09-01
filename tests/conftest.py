import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="fetch_nfe_test_")
os.environ.setdefault("STATE_DIR", os.path.join(_tmp, "state"))
os.environ.setdefault("DATA_DIR", os.path.join(_tmp, "data"))
os.environ.setdefault("CERTS_DIR", os.path.join(_tmp, "certs"))
