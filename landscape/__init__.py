DEBIAN_REVISION = ""
UPSTREAM_VERSION = "1.3.2.1"
VERSION = "%s%s" % (UPSTREAM_VERSION, DEBIAN_REVISION)
API = "3.2"

import pwd

from landscape.lib.initgroups import initgroups as cinitgroups
from twisted.python import util

def initgroups(uid, gid):
    return cinitgroups(pwd.getpwuid(uid).pw_name, gid)

util.initgroups = initgroups
