"""Profile-scoped Hugging Face credentials, sent over SSH stdin only."""
import hashlib
import hmac
import shlex


def credential_commands(app_secret: str, profile_id: int):
    identity = hmac.new(app_secret.encode(), str(profile_id).encode(), hashlib.sha256).hexdigest()
    relative = f'.cache/aig-benchmark-control/hf-auth/{identity}.token'
    source = (
        'import os,pathlib,sys; os.umask(0o077); '
        f'p=pathlib.Path.home()/{relative!r}; '
        'p.parent.mkdir(parents=True,exist_ok=True,mode=0o700); '
        'fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600); '
        'os.fchmod(fd,0o600); '
        'f=os.fdopen(fd,"w"); f.write(sys.stdin.read()); f.close()'
    )
    write_command = '~/.aig-vllm/bin/python -c ' + shlex.quote(source)
    # Set before importing HF libraries; never put a bearer token in process argv.
    environment = (
        'unset HF_TOKEN HUGGING_FACE_HUB_TOKEN HF_DEBUG\n'
        f'export HF_TOKEN_PATH="$HOME/{relative}"\n'
        'export HF_HUB_DISABLE_IMPLICIT_TOKEN=0\n'
        'export HF_HUB_VERBOSITY=warning\n'
    )
    return write_command, environment
