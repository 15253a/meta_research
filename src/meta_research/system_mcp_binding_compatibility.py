"""Execution-only bridge for the reviewed 8769 system MCP deployment.

Never rewrite admission bindings, source provenance, receipts or operation
identities. Only the exact captured source/instruction revisions below bridge
this additive transport change. Every other binding field remains exact.
"""
from dataclasses import replace
from typing import Any


_REVIEWED_REVISIONS: tuple[dict[str, Any], ...] = ({'before': {'binding_type': 'IdeaRuntimeBinding',
             'profile': None,
             'instruction_set_hash': '7871d5d09d66e5bd6d1e6465f5afe7fd6d0d8d4922f2394f4475f63ea42d109f',
             'sources': ['adapter-source:meta_research.idea_skill@sha256:653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'IdeaRuntimeBinding',
            'profile': None,
            'instruction_set_hash': '80a7ad63698f69275dd142559fcae16f996d95f372260d5c06300b81dc68c18d',
            'sources': ['adapter-source:meta_research.idea_skill@sha256:63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'PlanRuntimeBinding',
             'profile': None,
             'instruction_set_hash': 'adfef6b5e15b65b51b9198579f4807dbf851e9f8cec99df7edbe32d9f6c8315d',
             'sources': ['adapter-source:meta_research.plan_skill@sha256:03ab1bc17b8bfc35089df02e5603a2605a8de466c2c0c7de49b4a44931f45c75',
                         'adapter-source:meta_research.idea_skill@sha256:653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'PlanRuntimeBinding',
            'profile': None,
            'instruction_set_hash': 'c550f77bf5fe7e81633953b873637d869135651981a914bfc007c3d487bc6b87',
            'sources': ['adapter-source:meta_research.plan_skill@sha256:179f512a5da940b1c686ae3cc44e916118a302a0e588d2a0c187e878add9d100',
                        'adapter-source:meta_research.idea_skill@sha256:63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'BundleRuntimeBinding',
             'profile': None,
             'instruction_set_hash': '43b209abaecac3a4b8c696c6449df1d2cb7c355403992459ca81d52a8c4e7274',
             'sources': ['adapter-source:meta_research.bundle_dispatch_recovery@sha256:91a310631a65c2992e097d96b082904c4dc4366a3afb7c610b4c1fe42556def5',
                         'adapter-source:meta_research.bundle_skill@sha256:a68b18ac34873d2cb3357938bbdf23a5b9d7c3e54926b6806b396cff2ce0246e',
                         'adapter-source:meta_research.idea_skill@sha256:653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'BundleRuntimeBinding',
            'profile': None,
            'instruction_set_hash': '25e036c35747d55a0262a38f9d82c2cc80e30675ce7e1745f8cfda581cda9bde',
            'sources': ['adapter-source:meta_research.bundle_dispatch_recovery@sha256:91a310631a65c2992e097d96b082904c4dc4366a3afb7c610b4c1fe42556def5',
                        'adapter-source:meta_research.bundle_skill@sha256:e95f80f756751e7d28800cb7390e40a687c1b7b692b9ae855f58c7b71205854e',
                        'adapter-source:meta_research.idea_skill@sha256:63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'ReasoningRuntimeBinding',
             'profile': None,
             'instruction_set_hash': '233ae7b7ad3648dd2345eec867a2cbdf5ad547007f4739bc845959d60cda7057',
             'sources': ['adapter-source:meta_research.reasoning_skill@sha256:dc4154a169297bb7cdecd6ab2dea66544b447b02ffe93f4fe876a909e20a5715',
                         'adapter-source:meta_research.idea_skill@sha256:653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'ReasoningRuntimeBinding',
            'profile': None,
            'instruction_set_hash': 'dae700cced1efb6c42b2e36b80be1eeffe7271c76e7731c4eb68fac0e3f53fd4',
            'sources': ['adapter-source:meta_research.reasoning_skill@sha256:4940f1f7202ce2c1f762946e850fcb26f8738820ee9ce35c92cf7a4630e8eb88',
                        'adapter-source:meta_research.idea_skill@sha256:63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'WritingRuntimeBinding',
             'profile': 'report',
             'instruction_set_hash': '5b39d97a727c2369b0ee5e7b599a9eb2ba4c0a53d3366620582787828907d8cb',
             'sources': ['adapter-source:meta_research.writing_skill@sha256:19415372a7d1a42845ba07f0729280ef881933b266b639384b4c80e33b383c42',
                         'adapter-source:meta_research.idea_skill@sha256:653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'WritingRuntimeBinding',
            'profile': 'report',
            'instruction_set_hash': 'd71819ec20b006c39cbb0ad8f0984b559b56f1a2c33204e9540d6aa242a95ba8',
            'sources': ['adapter-source:meta_research.writing_skill@sha256:90d484e00df31723cd348c162076fd119a25fa3f9f1f8a16986574be70576b2f',
                        'adapter-source:meta_research.idea_skill@sha256:63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'WritingRuntimeBinding',
             'profile': 'paper',
             'instruction_set_hash': '89658c6065791b86d03926e0def2d0361dd4cf83f5791e6e9a63be43ec7f7433',
             'sources': ['adapter-source:meta_research.writing_skill@sha256:19415372a7d1a42845ba07f0729280ef881933b266b639384b4c80e33b383c42',
                         'adapter-source:meta_research.idea_skill@sha256:653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'WritingRuntimeBinding',
            'profile': 'paper',
            'instruction_set_hash': 'dd1f2e9b50c877099fe36ecaa2f23b728a9626bf1463f3b63ed01f63dcd064fc',
            'sources': ['adapter-source:meta_research.writing_skill@sha256:90d484e00df31723cd348c162076fd119a25fa3f9f1f8a16986574be70576b2f',
                        'adapter-source:meta_research.idea_skill@sha256:63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'WritingRuntimeBinding',
             'profile': 'presentation',
             'instruction_set_hash': '860564e0efc1b92024b8f9ce18c10e8b85c09b256c950b88210750b7289ed2e7',
             'sources': ['adapter-source:meta_research.writing_skill@sha256:19415372a7d1a42845ba07f0729280ef881933b266b639384b4c80e33b383c42',
                         'adapter-source:meta_research.idea_skill@sha256:653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'WritingRuntimeBinding',
            'profile': 'presentation',
            'instruction_set_hash': '7249e6b7f8d6dd0cc2a95a3e99073becc8257c4aafb345630949cae576e58f5b',
            'sources': ['adapter-source:meta_research.writing_skill@sha256:90d484e00df31723cd348c162076fd119a25fa3f9f1f8a16986574be70576b2f',
                        'adapter-source:meta_research.idea_skill@sha256:63518da94839dcf74332db7280caaffa9104dffbe2aa64b102dab9eb12103f44',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}})


def previous_system_mcp_binding(binding: Any) -> Any | None:
    sources = [entry for entry in binding.resource_bindings if entry.startswith("adapter-source:")]
    for pair in _REVIEWED_REVISIONS:
        before, after = pair["before"], pair["after"]
        if (
            type(binding).__name__ != after["binding_type"]
            or binding.instruction_set_hash != after["instruction_set_hash"]
            or sources != after["sources"]
        ):
            continue
        replacements = dict(zip(after["sources"], before["sources"], strict=True))
        return replace(binding,
            instruction_set_hash=before["instruction_set_hash"],
            resource_bindings=tuple(replacements.get(entry, entry) for entry in binding.resource_bindings),
        )
    return None


def system_mcp_bindings_compatible(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if left == right:
        return True
    before = previous_system_mcp_binding(left)
    after = previous_system_mcp_binding(right)
    return (left if before is None else before) == (right if after is None else after)
