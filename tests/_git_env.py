"""Run every git command a test starts under the suite's own global git configuration.

Imported for its effect by each test module that runs git, directly or through the program
it tests. Setting the variable in this process's environment reaches every subprocess the
tests start, the programs under test included, so no call site has to remember it.

`GIT_CONFIG_GLOBAL` replaces both `~/.gitconfig` and `$XDG_CONFIG_HOME/git/config`: a
developer's commit signing, identity routing or other defaults cannot fail a test on their
machine alone. The system-level config is kept, because Git for Windows sets line-ending
behavior there that the suite runs under on that platform.
"""

import os
from pathlib import Path

GITCONFIG = Path(__file__).resolve().parent / "fixtures" / "gitconfig"

os.environ["GIT_CONFIG_GLOBAL"] = str(GITCONFIG)
