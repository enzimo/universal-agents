# HF CLI reference

Reviewed against huggingface-hub 1.28.0. The upstream Apache-2.0 project and guide:
https://github.com/huggingface/huggingface_hub
https://huggingface.co/docs/hub/agents-cli

This curated reference was checked against installed help and `hf skills preview`.
It retains only commands relevant to the UA workflow. It is not an unmodified
upstream skill, whose authentication and global installation instructions do not
implement UA's broker boundary.

```bash
hf version
hf models ls --search bert --limit 3 --json
hf datasets ls --search wikipedia --limit 3 --json
hf models ls OWNER/REPO --json
hf download OWNER/REPO config.json --revision COMMIT --local-dir ./artifacts/model
```

For public operations set `HF_HUB_DISABLE_IMPLICIT_TOKEN=1` and
`HF_HUB_DISABLE_UPDATE_CHECK=1`. Use the broker for authenticated operations.
Run `hf <command> --help` when selecting flags; do not run `hf skills add`,
`hf skills update`, or `hf update` inside managed images. Update the pinned
package and reviewed shared/bundled skills together through source changes.

Generated upstream skill SHA-256 at review: `31d4fc2d85b39129a520abe359237fc94c71ddf91f448fbeb16071ce96e2e232`.
