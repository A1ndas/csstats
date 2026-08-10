from gevent import monkey; monkey.patch_all()   # MUST be the first statement

import sys

from csstats.cli import main

if __name__ == "__main__":
    sys.exit(main())