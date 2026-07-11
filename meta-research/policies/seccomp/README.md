# Pinned seccomp profile

`moby-default-v0.2.1.json` is a canonical minified copy of Moby's Apache-2.0
`seccomp/v0.2.1` default profile:

- source: `https://github.com/moby/profiles/blob/seccomp/v0.2.1/seccomp/default.json`
- upstream raw SHA-256: `536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74`
- canonical vendored SHA-256: `ea7ba4298390d31a9d52354cf4319e4fe6cbd717ff6b47f23a496ca169727c76`

The canonical copy preserves the parsed JSON semantics while sorting object keys
and removing insignificant whitespace. Update the policy hash and runtime
identity only through a reviewed profile upgrade.

`moby-default-v0.2.1-linux-5.4-amd64.bpf.b64` is the libseccomp 2.5.3 export
for an amd64, Linux 5.4, cap-drop-all evaluation of that profile. Its decoded
SHA-256 is `4fb43ea7bb76d9462eb73270fb52e473fb4423bfe09040bfa16c255a7eb133f2`.
Regenerate it with `scripts/generate_seccomp_bpf.py`; byte equality is required.

The trusted pinned-image launcher installs this BPF after hard rlimits and
before parsing payload environment or executing payload code. This is the
authoritative syscall boundary because the deployment platform replaces
Docker's requested/default profile with an additive host policy.
