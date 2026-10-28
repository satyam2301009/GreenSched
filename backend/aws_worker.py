import os
import json
import uuid
import shlex
import posixpath
import paramiko


def _connect():
    host = os.getenv("AWS_HOST", "15.134.215.24")
    username = os.getenv("AWS_USER", "ec2-user")
    key_path = os.getenv("AWS_KEY_PATH", r"I:\GreenSched\greensched-key.pem")

    if not os.path.isfile(key_path):
        raise FileNotFoundError(f"AWS private key not found: {key_path}")

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        hostname=host,
        username=username,
        key_filename=key_path,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run_python_script(script: str, task_id: str | None = None):
    if not isinstance(script, str) or not script.strip():
        raise ValueError("Python script must not be empty")
    if len(script) > 20000:
        raise ValueError("Python script exceeds the 20,000-character limit")

    safe_id = "".join(ch for ch in (task_id or uuid.uuid4().hex) if ch.isalnum())[:32]
    if not safe_id:
        safe_id = uuid.uuid4().hex
    remote_dir = "/home/ec2-user/greensched-worker/jobs"
    remote_path = posixpath.join(remote_dir, f"{safe_id}.py")
    client = _connect()

    try:
        # Directory path and script filename are fixed/generated, not user-controlled.
        _, mkdir_out, mkdir_err = client.exec_command(
            "mkdir -p /home/ec2-user/greensched-worker/jobs && chmod 700 /home/ec2-user/greensched-worker/jobs",
            timeout=15,
        )
        if mkdir_out.channel.recv_exit_status() != 0:
            raise RuntimeError(mkdir_err.read().decode("utf-8", errors="replace"))

        sftp = client.open_sftp()
        try:
            with sftp.file(remote_path, "w") as remote_file:
                remote_file.write(script)
            sftp.chmod(remote_path, 0o600)
        finally:
            sftp.close()

        # `timeout` caps runtime; the quoted path is generated from a sanitized ID.
        command = f"timeout 120s python3 -I {shlex.quote(remote_path)}"
        stdin, stdout, stderr = client.exec_command(command, timeout=130)
        out_text = stdout.read().decode("utf-8", errors="replace")
        err_text = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        result = {
            "status": "completed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "stdout": out_text[-10000:],
            "stderr": err_text[-5000:],
            "execution_location": "AWS EC2 Sydney",
            "remote_script": remote_path,
        }
        return result
    finally:
        client.close()


def run_cloud_test():
    return run_python_script(
        "import json, platform, time\\n"
        "from datetime import datetime, timezone\\n"
        "start=time.time()\\n"
        "limit=100000\\n"
        "count=0\\n"
        "for n in range(2, limit+1):\\n"
        "    prime=True\\n"
        "    for d in range(2, int(n**0.5)+1):\\n"
        "        if n%d==0:\\n"
        "            prime=False\\n"
        "            break\\n"
        "    if prime: count+=1\\n"
        "print(json.dumps({'task':'GreenSched Cloud CPU Test','status':'completed',"
        "'executed_at_utc':datetime.now(timezone.utc).isoformat(),"
        "'hostname':platform.node(),'architecture':platform.machine(),"
        "'python_version':platform.python_version(),'numbers_checked':limit-1,"
        "'prime_count':count,'execution_seconds':round(time.time()-start,3)}, indent=2))"
    )
