import itertools
import json
import os
import re
import select
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from winpty import PtyProcess


wrapper = '''read -rp "Use TypeScript? [y/N] " ts </dev/tty; read -rp "Use Tailwind CSS? [y/N] " tw </dev/tty; language=--js; css=--no-tailwind; case "$ts" in [yY]) language=--ts;; esac; case "$tw" in [yY]) css=--tailwind;; esac; exec "$@" "$language" "$css"'''
arguments = ['pnpm', 'create', 'next-app@16.3.6', '.', '--app', '--no-src-dir', '--no-eslint', '--import-alias', '@/*', '--react-compiler']
prompts = ['Use TypeScript? [y/N] ', 'Use Tailwind CSS? [y/N] ']
results = []
output_directory = Path('test-results').resolve()
output_directory.mkdir()


def run_case(name, directory, command_arguments, answers, timeout=30):
    command = shlex.join(['bash', '-ec', wrapper, '_', *command_arguments])
    script = directory / 'run.sh'
    script.write_text(command + '\n', encoding='utf-8')
    print(f'COMMAND {name}: {command}', flush=True)
    process = PtyProcess.spawn(
        [os.environ['TEST_GIT_BASH'], '--noprofile', '--norc', script.as_posix()],
        cwd=str(directory),
        dimensions=(40, 240),
    )
    transcript = ''
    sent = 0
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([process.fileobj], [], [], 0.1)
            if readable:
                try:
                    transcript += process.read(65536)
                except EOFError:
                    break
                clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', transcript)
                if sent < len(answers) and prompts[sent] in clean:
                    process.write(answers[sent])
                    sent += 1
            elif not process.isalive():
                break
        else:
            raise AssertionError(f'{name}: timed out after {timeout} seconds')
        assert sent == len(answers), f'{name}: only {sent} of {len(answers)} prompts answered'
        exit_code = process.wait()
        print(f'EXIT {name}: {exit_code}', flush=True)
        return exit_code
    finally:
        (output_directory / f'{name}.log').write_text(transcript, encoding='utf-8')
        print(re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', transcript), flush=True)
        if process.isalive():
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=True)


def selected_flags(answers):
    return [
        '--ts' if answers[0].lower() == 'y' else '--js',
        '--tailwind' if answers[1].lower() == 'y' else '--no-tailwind',
    ]


with tempfile.TemporaryDirectory(prefix='next-app-prompts-') as temporary:
    root = Path(temporary)
    mock = root / 'mock-pnpm.sh'
    mock.write_text('printf "%s\\n" "$@" > arguments.txt\nexit "${MOCK_EXIT:-0}"\n', encoding='utf-8')
    for answers in [*itertools.product(['n', 'y'], repeat=2), ('', ''), ('Y', 'Y')]:
        name = 'mock-' + '-'.join(answer or 'enter' for answer in answers)
        directory = root / name
        directory.mkdir()
        command_arguments = ['bash', mock.as_posix(), *arguments[1:]]
        assert run_case(name, directory, command_arguments, [answer + '\r' for answer in answers]) == 0
        actual = (directory / 'arguments.txt').read_text().splitlines()
        assert actual == arguments[1:] + selected_flags(answers), actual
        results.append({'name': name, 'flags': actual[-2:]})
        print(f'PASS {name}: {actual[-2:]}', flush=True)

    for name, control in [('cancel', '\x03'), ('eof', '\x04')]:
        directory = root / name
        directory.mkdir()
        assert run_case(name, directory, ['bash', mock.as_posix()], [control]) != 0
        assert not (directory / 'arguments.txt').exists()
        results.append({'name': name, 'invoked': False})
        print(f'PASS {name}: no command invocation', flush=True)

    directory = root / 'failure'
    directory.mkdir()
    os.environ['MOCK_EXIT'] = '7'
    assert run_case('failure', directory, ['bash', mock.as_posix()], ['n\r', 'n\r']) == 7
    del os.environ['MOCK_EXIT']
    results.append({'name': 'failure', 'exit': 7})
    print('PASS failure: exit status 7 preserved', flush=True)

    for answers in itertools.product(['n', 'y'], repeat=2):
        name = 'create-next-app-' + '-'.join(answers)
        directory = root / name
        directory.mkdir()
        assert run_case(
            name,
            directory,
            [*arguments, '--skip-install', '--disable-git'],
            [answer + '\r' for answer in answers],
            timeout=240,
        ) == 0
        package = json.loads((directory / 'package.json').read_text())
        dev_dependencies = package['devDependencies']
        typescript = answers[0] == 'y'
        tailwind = answers[1] == 'y'
        assert ('typescript' in dev_dependencies) == typescript, dev_dependencies
        assert ('tailwindcss' in dev_dependencies) == tailwind, dev_dependencies
        assert 'eslint' not in dev_dependencies, dev_dependencies
        config = 'tsconfig.json' if typescript else 'jsconfig.json'
        assert json.loads((directory / config).read_text())['compilerOptions']['paths'] == {'@/*': ['./*']}
        assert (directory / 'app' / ('layout.tsx' if typescript else 'layout.js')).exists()
        assert not (directory / 'src').exists()
        next_config = directory / ('next.config.ts' if typescript else 'next.config.mjs')
        assert 'reactCompiler: true' in next_config.read_text()
        result = {
            'name': name,
            'flags': selected_flags(answers),
            'typescript': typescript,
            'tailwind': tailwind,
            'config': config,
            'importAlias': '@/*',
            'reactCompiler': True,
        }
        results.append(result)
        print('PASS ' + json.dumps(result), flush=True)

(output_directory / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
print(f'All {len(results)} Windows Git Bash checks passed', flush=True)
