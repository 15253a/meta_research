"""Execution-only bridge for the reviewed 8769 system MCP deployment.

Never rewrite admission bindings, source provenance, receipts or operation
identities. Only the exact reviewed source/instruction revisions below bridge
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


# 2026-09-29: v1-test 829b701 plus the reviewed System MCP integration.
# These are recomputed Git source/instruction identities, not invented deployed
# bindings. The v1-test skills, output schemas and root profile are unchanged
# across this transport-only pair. Their full binding fields, including the
# seal key and capability grants, must still compare exactly. The original 8769
# bridge above remains immutable; this does not authorize its older profile or
# Reasoning schema to execute as the new research policy. Source evidence:
# tests/fixtures/system_mcp_test_merge_sources.json.
_REVIEWED_TEST_MERGE_REVISIONS: tuple[dict[str, Any], ...] = ({'before': {'binding_type': 'IdeaRuntimeBinding',
             'profile': None,
             'instruction_set_hash': '7d6e03322602a792c11ae0bdd6db6ef674f5f64c4ac0f7cbd98ca86ab8a750ae',
             'sources': ['adapter-source:meta_research.idea_skill@sha256:e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'IdeaRuntimeBinding',
            'profile': None,
            'instruction_set_hash': '26c73db315a99716019adb871787a357dc000b1270f39d98c9376f6c99c8b8de',
            'sources': ['adapter-source:meta_research.idea_skill@sha256:fffedc566be7dc9c82dea96378e712d38f4aebe751432c7a60dd72118d657936',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'PlanRuntimeBinding',
             'profile': None,
             'instruction_set_hash': '6d32653bb1048332c1fb4969556dfdf3a71b75b7d8c5931b5e46f2b6b748ae5c',
             'sources': ['adapter-source:meta_research.plan_skill@sha256:03ab1bc17b8bfc35089df02e5603a2605a8de466c2c0c7de49b4a44931f45c75',
                         'adapter-source:meta_research.idea_skill@sha256:e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'PlanRuntimeBinding',
            'profile': None,
            'instruction_set_hash': 'abee90ed77a5c79a3342aede44171373d9f017e5309a222b0171ee386ddaa023',
            'sources': ['adapter-source:meta_research.plan_skill@sha256:179f512a5da940b1c686ae3cc44e916118a302a0e588d2a0c187e878add9d100',
                        'adapter-source:meta_research.idea_skill@sha256:fffedc566be7dc9c82dea96378e712d38f4aebe751432c7a60dd72118d657936',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'BundleRuntimeBinding',
             'profile': None,
             'instruction_set_hash': '484957d57d243859af2ba2caccf925b2e620ab4989e4647be67f8db01294a1da',
             'sources': ['adapter-source:meta_research.bundle_dispatch_recovery@sha256:91a310631a65c2992e097d96b082904c4dc4366a3afb7c610b4c1fe42556def5',
                         'adapter-source:meta_research.bundle_skill@sha256:524ffd9419ee36ca50e9a58da740234caf8bd2d5c55deec51c3b17b4439e3335',
                         'adapter-source:meta_research.idea_skill@sha256:e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'BundleRuntimeBinding',
            'profile': None,
            'instruction_set_hash': '91337d0c986f99d9b5159f81d9bbd9c081e061124ada2d82b285a66e62eb47b3',
            'sources': ['adapter-source:meta_research.bundle_dispatch_recovery@sha256:91a310631a65c2992e097d96b082904c4dc4366a3afb7c610b4c1fe42556def5',
                        'adapter-source:meta_research.bundle_skill@sha256:e7a6501f533ba3d35e7fccfcaaf6ae4d6c2ff931f18b43e59b0eddb23b9c732a',
                        'adapter-source:meta_research.idea_skill@sha256:fffedc566be7dc9c82dea96378e712d38f4aebe751432c7a60dd72118d657936',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'ReasoningRuntimeBinding',
             'profile': None,
             'instruction_set_hash': 'd17bf1c909539be7311cdc3beab29b75e2283b825025826a8c452c2b0823d6bd',
             'sources': ['adapter-source:meta_research.reasoning_skill@sha256:4413a4af6b88670f5c06939a25246835cec3f1af22f45198ae0d007486f4d442',
                         'adapter-source:meta_research.idea_skill@sha256:e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'ReasoningRuntimeBinding',
            'profile': None,
            'instruction_set_hash': '1ac6c9957680243fccb4d6898733904f67faba7cd32afd563ae5cada48ea4a14',
            'sources': ['adapter-source:meta_research.reasoning_skill@sha256:082c7248dea06a39a946d74296b1d271e7acf445dd888df9d20d4194e95a94dd',
                        'adapter-source:meta_research.idea_skill@sha256:fffedc566be7dc9c82dea96378e712d38f4aebe751432c7a60dd72118d657936',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'WritingRuntimeBinding',
             'profile': 'report',
             'instruction_set_hash': '2dee231f549885ab12734fa86665099acd77e3f19a0dda9afea182d5646f3ca0',
             'sources': ['adapter-source:meta_research.writing_skill@sha256:19415372a7d1a42845ba07f0729280ef881933b266b639384b4c80e33b383c42',
                         'adapter-source:meta_research.idea_skill@sha256:e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'WritingRuntimeBinding',
            'profile': 'report',
            'instruction_set_hash': '2d5bc374d2e01ae751bc242daa7f5dfbcb37c43f977051ba3835ad909ba94ff0',
            'sources': ['adapter-source:meta_research.writing_skill@sha256:90d484e00df31723cd348c162076fd119a25fa3f9f1f8a16986574be70576b2f',
                        'adapter-source:meta_research.idea_skill@sha256:fffedc566be7dc9c82dea96378e712d38f4aebe751432c7a60dd72118d657936',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'WritingRuntimeBinding',
             'profile': 'paper',
             'instruction_set_hash': 'cbb5eadf32b7df0103fe8a0c6b2cf13073993f7e352af167d05d1b403a177d50',
             'sources': ['adapter-source:meta_research.writing_skill@sha256:19415372a7d1a42845ba07f0729280ef881933b266b639384b4c80e33b383c42',
                         'adapter-source:meta_research.idea_skill@sha256:e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'WritingRuntimeBinding',
            'profile': 'paper',
            'instruction_set_hash': '45925ccde9f088e12d5519a6e07fc26d7ec5f04bfb7470da073adbc05145b375',
            'sources': ['adapter-source:meta_research.writing_skill@sha256:90d484e00df31723cd348c162076fd119a25fa3f9f1f8a16986574be70576b2f',
                        'adapter-source:meta_research.idea_skill@sha256:fffedc566be7dc9c82dea96378e712d38f4aebe751432c7a60dd72118d657936',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}},
 {'before': {'binding_type': 'WritingRuntimeBinding',
             'profile': 'presentation',
             'instruction_set_hash': '3a106dd1364a62018d1f4953589ca5d8bd97690fb54f20af9885d77db003e263',
             'sources': ['adapter-source:meta_research.writing_skill@sha256:19415372a7d1a42845ba07f0729280ef881933b266b639384b4c80e33b383c42',
                         'adapter-source:meta_research.idea_skill@sha256:e20a48af0c526466baa2303cd1e9d4f028399da4389faca8b62e13439036d699',
                         'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                         'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']},
  'after': {'binding_type': 'WritingRuntimeBinding',
            'profile': 'presentation',
            'instruction_set_hash': '3b34c5db8fcdfd9eebe82248dec1ef484fd01491878e536d4b270b50edec63ac',
            'sources': ['adapter-source:meta_research.writing_skill@sha256:90d484e00df31723cd348c162076fd119a25fa3f9f1f8a16986574be70576b2f',
                        'adapter-source:meta_research.idea_skill@sha256:fffedc566be7dc9c82dea96378e712d38f4aebe751432c7a60dd72118d657936',
                        'adapter-source:meta_research.root_resident_mcp@sha256:f87d3afd46e3b0c4459b1910e06ed3772d49aa28bf470c9bd624e5b157fd051f',
                        'adapter-source:meta_research.provider_supervisor@sha256:b2c92ca7eba00714ed4bdfbe549f0977a7a4603101d0a77d2f8e1352c3da3f4c']}})


def previous_system_mcp_binding(binding: Any) -> Any | None:
    sources = [entry for entry in binding.resource_bindings if entry.startswith("adapter-source:")]
    for pair in (*_REVIEWED_REVISIONS, *_REVIEWED_TEST_MERGE_REVISIONS):
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
