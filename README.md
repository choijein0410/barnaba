This isolated audit branch compares srnas/barnaba PR 63 against its unchanged
upstream base. It does not alter the PR branch or upstream repository.

The Ubuntu 24.04 x86_64 runner, exact Python versions, checkout/setup-python
action commits, and all runtime/test package versions are taken from upstream
run 37210773578, attempt 2. The runner image version is checked explicitly.
Build-isolation tooling was not fully recorded in the original logs.

Each Python job runs the pristine upstream suite, then the PR suite in a
separate source directory with the same installed dependencies. Finally, only
the eight new regression tests are run against the upstream source. The audit
compares all shared outcomes and failure messages, verifies the original CI
failure list, and preserves JUnit reports, full logs, source hashes and package
versions as artifacts. A successful audit means the expected baseline failures
were reproduced identically; it does not mean Barnaba's full suite passes.
