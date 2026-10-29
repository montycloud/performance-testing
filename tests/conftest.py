# Locust needs gevent's monkey-patching before ssl/urllib3 are imported anywhere.
from gevent import monkey

monkey.patch_all()
