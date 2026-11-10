"""A native ELF provider boundary for protected creation integration tests."""
import json
import shutil
import subprocess
import sys

from meta_research.codex_runtime import CODEX_LOCKED_VERSION
from meta_research.companion import CodexCompanionAdapter


QUESTION = {"title": "Compare calibration", "unknown_statement": "Does the cold condition reproduce the effect?",
    "answer_shape": "A controlled comparison", "applicability_scope": "Small calibration experiment",
    "background_context": "Prior calibration motivates a comparison.", "requirements_constraints": "Keep conditions explicit"}


class ObservedCompanion(CodexCompanionAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_calls = []
        self.native_markers = []

    def _protected_creation_invoke(self, request, **kwargs):
        result = super()._protected_creation_invoke(request, **kwargs)
        self.native_calls.append((kwargs["operation_name"], result[-1]))
        self.native_markers.append(json.loads((result[-1].work_directory / "native-marker.txt").read_text()))
        return result


def native_creation_executable(path):
    compiler = shutil.which("cc")
    assert compiler is not None, "Native integration requires a C compiler"
    code = r"""import json, os, sys
from pathlib import Path
if '--version' in sys.argv:
    print('codex-cli VERSION'); raise SystemExit(0)
if sys.argv[-2:] == ['features', 'list']:
    print('hooks stable true\nmulti_agent stable true\nplugins stable true\nremote_plugin stable true\nshell_tool stable true\nskill_search stable true\nunified_exec stable true'); raise SystemExit(0)
prompt = sys.stdin.read()
marker = 'Protected creation binding and explicit guidance file locations:'
bound = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1].lstrip())[0]
work = Path(bound['execution']['work_directory']); work.mkdir(parents=True, exist_ok=True)
for guidance in bound['guidance_files'].values():
    assert Path(guidance).read_text()
previous = len(list(Path('/workspace/operations').glob('*/native-marker.txt')))
(work / 'native-marker.txt').write_text(json.dumps({'cwd': os.getcwd(), 'previous_operations': previous}))
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
if 'proposal_fork_native_session_ref' in schema['properties']:
    value = {'proposal_fork_native_session_ref': 'native-child-' + work.name, 'result': {'content': QUESTION, 'revision': None}}
elif 'coverage' in schema['properties']:
    value = {key: [] for key in schema['properties']}
    value['coverage'] = [{'material_key': item['reference_ref'], 'kind': 'unread', 'read_ranges': [], 'unread_description': 'Recorded for subsequent research; no source read claimed.'} for item in bound['material_references']['references']]
else:
    value = {'reply': 'Checked pending observations.'}
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(value))
print(json.dumps({'type': 'thread.started', 'thread_id': 'local-native-root'}))
print(json.dumps({'type': 'turn.completed'}))
""".replace('VERSION', CODEX_LOCKED_VERSION).replace('QUESTION', repr(QUESTION))
    source = path.with_suffix('.c')
    source.write_text('#include <unistd.h>\n#include <stdlib.h>\nint main(int argc, char **argv) {\n'
        + 'const char *code=' + json.dumps(code) + ';\n'
        + 'char **args=calloc(argc+3,sizeof(char*));\n'
        + 'args[0]=access("/python/bin/python3",X_OK)==0?"/python/bin/python3":' + json.dumps(sys.executable) + ';\n'
        + 'args[1]="-c";args[2]=(char*)code;for(int i=1;i<argc;i++)args[i+2]=argv[i];\n'
        + 'execv(args[0],args);return 127;}\n')
    subprocess.run([compiler, str(source), '-o', str(path)], check=True, capture_output=True)
    path.chmod(0o755)
    return path
