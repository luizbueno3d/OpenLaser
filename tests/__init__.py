import os
import tempfile

# Keep the suite, and the job processes it spawns, off the real board lock
# (~/Library/Application Support/OpenLaser/board.lock), which a running
# OpenLaser or `ol` may hold.
os.environ["OPENLASER_BOARD_LOCK"] = os.path.join(tempfile.mkdtemp(prefix="openlaser-tests-"), "board.lock")
