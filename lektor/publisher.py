
from _future_ import annotations

import errno
import hashlib
import io
import os
import posixpath
import urllib.parse
from contextlib import contextmanager, ExitStack, suppress
from ftplib import Error as FTPError
from inspect import cleandoc
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, DEVNULL, PIPE, STDOUT
from tempfile import TemporaryDirectory
from typing import Any, Callable, Generator, Iterator, Mapping, Sequence
from urllib.parse import urlsplit
from warnings import warn

from werkzeug.datastructures import MultiDict

from lektor.compat import werkzeug_urls_URL
from lektor.exception import LektorException
from lektor.utils import bool_from_string, locate_executable, portable_popen

def _parse_query(query: str, **kwargs: Any) -> MultiDict:
    return MultiDict(urllib.parse.parse_qsl(query, **kwargs))

def _ascii_host(host: str) -> str:
    return host.encode("idna").decode("ascii")

@contextmanager
def _ssh_key_file(credentials: Mapping[str, str] | None) -> Iterator[str | None]:
    with ExitStack() as stack:
        key_file = credentials.get("key_file") if credentials else None
        key = credentials.get("key") if credentials else None
        if not key_file and key:
            if ":" in key:
                key_type, _, key = key.partition(":")
                key_type = key_type.upper()
            else:
                key_type = "RSA"
            key_file = Path(stack.enter_context(TemporaryDirectory()), "keyfile")
            with key_file.open("w", encoding="utf-8") as f:
                f.write(f"-----BEGIN {key_type} PRIVATE KEY-----\n")
                f.writelines(key[x : x + 64] + "\n" for x in range(0, len(key), 64))
                f.write(f"-----END {key_type} PRIVATE KEY-----\n")
        yield str(key_file) if key_file else None

@contextmanager
def _ssh_command(credentials: Mapping[str, str] | None, port: int | None = None) -> Iterator[str | None]:
    with _ssh_key_file(credentials) as key_file:
        args = []
        if port:
            args.append(f" -p {port}")
        if key_file:
            args.append(f' -i "{key_file}" -o IdentitiesOnly=yes')
        yield "ssh" + " ".join(args) if args else None

class PublishError(LektorException):
    pass

class Command:
    def _init_(self, argline, *, cwd=None, env=None, capture=True, silent=False, check=False, input=None, capture_stdout=False):
        kwargs = {"cwd": cwd}
        if env:
            kwargs["env"] = {**os.environ, **env}
        if silent:
            kwargs["stdout"] = DEVNULL
            kwargs["stderr"] = DEVNULL
            capture = False
        if input is not None:
            kwargs["stdin"] = PIPE
        if capture or capture_stdout:
            kwargs["stdout"] = PIPE
        if capture:
            kwargs["stderr"] = STDOUT if not capture_stdout else PIPE
        kwargs["text"] = True
        kwargs["errors"] = "replace"

        self.capture = capture
        self.check = check
        self._stdout = None

        with ExitStack() as stack:
            self._cmd = stack.enter_context(portable_popen(list(argline), **kwargs))
            self._closer = stack.pop_all().close

        if input is not None or capture_stdout:
            self._output = self._communicate(input, capture_stdout, capture)
        elif capture:
            self._output = self._cmd.stdout
        else:
            self._output = None

    def _communicate(self, input, capture_stdout, capture):
        proc = self._cmd
        try:
            if capture_stdout:
                self._stdout, errout = proc.communicate(input)
            else:
                errout, _ = proc.communicate(input)
        except BaseException:
            proc.kill()
            with suppress(CalledProcessError):
                self.close()
            raise
        return iter(errout.splitlines()) if capture else None

    def close(self):
        closer, self._closer = self._closer, None
        if closer:
            closer()
        if self.check:
            rc = self._cmd.poll()
            if rc != 0:
                raise CalledProcessError(rc, self._cmd.args, self._stdout)

    def wait(self):
        self._cmd.wait()
        self.close()
        return self._cmd.returncode

    def result(self):
        return CompletedProcess(self._cmd.args, self.wait(), self._stdout)

    @property
    def returncode(self):
        return self._cmd.returncode

    def _iter_(self):
        if self._output is None:
            raise RuntimeError("Not capturing")
        for line in self._output:
            yield line.rstrip()
        return self.result()

def publish(env, target, output_path, credentials=None, **extra):
    url = urlsplit(target)
    scheme = url.scheme
    output_path = os.path.abspath(output_path)

    if scheme == "rsync":
        return publish_rsync(env, output_path, target, credentials, **extra)
    elif scheme == "ftp":
        return publish_ftp(env, output_path, target, credentials, **extra)
    elif scheme.startswith("ghpages"):
        return publish_ghpages(env, output_path, target, credentials, **extra)
    else:
        raise PublishError(f"Unknown publishing scheme: {scheme}")

def publish_rsync(env, output_path, target_url, credentials, **extra):
    credentials = credentials or {}
    argline = ["rsync", "-rclzv", "--exclude=.lektor"]
    target = []
    env_vars = {}

    url = urlsplit(target_url)
    options = _parse_query(url.query, keep_blank_values=True)
    exclude = options.getlist("exclude")
    for file in exclude:
        argline.extend(("--exclude", file))

    delete = options.get("delete", False) in ("", "on", "yes", "true", "1", None)
    if delete:
        argline.append("--delete-after")

    with _ssh_command(credentials, url.port) as ssh_command:
        if ssh_command:
            argline.extend(("-e", ssh_command))

        username = credentials.get("username") or url.username
        if username:
            target.append(username + "@")
        if url.hostname:
            target.append(_ascii_host(url.hostname))
            target.append(":")
        target.append(url.path.rstrip("/") + "/")

        argline.append(output_path.rstrip("/\") + "/")
        argline.append("".join(target))

        with Command(argline, env=env_vars) as cmd:
            yield from cmd


def _prefix_output(lines: Iterable[str], prefix: str = "> ") -> Iterator[str]:
        """Add prefix to lines."""
   return (f"{prefix}{line}" for line in lines)

def publish_ghpages(
    env,
    output_path: str,
    target_url: str,
    credentials: Mapping[str, str] | None = None,
    cname: str | None = None,
    preserve_history: bool = True,
)   Iterator[str]:
    """Publish the contents of the output path to GitHub pages.

    :param env: The Lektor environment.
    :param output_path: The path to the generated website.
    :param target_url: The URL to push to (e.g., git@github.com:owner/repo.git#gh-pages).
    :param credentials: Optional credentials for the Git repository.
    :param cname: Optional. Create a top-level `CNAME` with given contents.
    :param preserve_history: Whether to preserve the existing git history.
    """
    if not locate_executable("git"):
        raise PublishError("git executable not found; cannot deploy.")

    url = urlsplit(target_url)
    fragment = url.fragment
    push_url_base = url._replace(fragment="").geturl()
    branch = fragment if fragment else "gh-pages"

    gh_owner = url.hostname.lower() if url.hostname else None
    gh_project = url.path.strip("/").lower() if url.path else None

    if not push_url_base:
        raise PublishError("Push URL is missing from the target.")
    if not gh_owner or not gh_project:
        warn("GitHub owner or project not clearly defined in target URL.", DeprecationWarning)

    params = _parse_query(url.query, keep_blank_values=True)
    cname_param = params.get("cname")
    branch_param = params.get("branch")
    preserve_history_param = bool_from_string(params.get("preserve_history"), True)

    if branch_param:
        branch = branch_param
    if cname_param:
        cname = cname_param
    preserve_history = preserve_history_param

    with TemporaryDirectory() as git_dir:
        environ = {**os.environ, "GIT_WORK_TREE": output_path, "GIT_DIR": git_dir}

        for what, default in [("NAME", "Lektor Bot"), ("EMAIL", "bot@getlektor.com")]:
            value = (
                environ.get(f"GIT_AUTHOR_{what}")
                or environ.get(f"GIT_COMMITTER_{what}")
                or default
            )
            for key in f"GIT_AUTHOR_{what}", f"GIT_COMMITTER_{what}":
                environ[key] = environ.get(key) or value

        with _ssh_command(credentials, url.port) as ssh_command:
            if ssh_command:
                environ.setdefault("GIT_SSH_COMMAND", ssh_command)

            username = credentials.get("username") or url.username
            password = credentials.get("password") or url.password
            if push_url_base.startswith("https:") and (username or password):
                userpass = f"{username}:{password}" if password else username
                cred_file_path = os.path.join(git_dir, "lektor_cred_file")
                with open(cred_file_path, "w", encoding="utf-8") as f:
                    f.write(f"https://{userpass}@{url.netloc}\n")
                run_command(["git", "config", "credential.helper", f'store --file "{cred_file_path}"'], env=environ)

            def run_command(args: Sequence[str], check: bool = True, input: str | None = None, capture_stdout: bool = False) -> CompletedProcess[str]:
                cmd = ["git"] + list(args)
                result = portable_popen(cmd, env=environ, text=True, capture_output=True, input=input)
                if check and result.returncode != 0:
                    raise CalledProcessError(result.returncode, cmd, stdout=result.stdout, stderr=result.stderr)
                return result

            yield from _prefix_output(run_command(["init", "--quiet"]))

            refspec = f"refs/heads/{branch}"
            if preserve_history:
                yield "Fetching existing head"
                fetch_result = run_command(["fetch", "--depth=1", push_url_base, refspec], check=False)
                yield from _prefix_output(fetch_result.stdout.splitlines())
                if fetch_result.returncode == 0:
                    yield from _prefix_output(run_command(["reset", "--soft", "FETCH_HEAD"]).stdout.splitlines())
                else:
                    yield f"Creating new branch {branch}"

            yield from _prefix_output(run_command(["add", "--force", "--all", "--", ".", ":(exclude).lektor"]))

            if cname is not None:
                run_command(["update-index", "--add", "--cacheinfo", "100644", run_command(["hash-object", "-w", "--stdin", input=f"{cname}\n"], capture_stdout=True).stdout.strip(), "CNAME"])

            diff_result = run_command(["diff", "--cached", "--no-renames", "--exit-code", "--quiet"], check=False)
            if diff_result.returncode == 0:
                yield "No changes to publish☺"
            elif diff_result.returncode == 1:
                yield "Creating commit"
                yield from _prefix_output(run_command(["commit", "--quiet", "--message", "Synchronized build"]).stdout.splitlines())
                push_args = ["push", push_url_base, f"HEAD:{refspec}"]
                if not preserve_history:
                    push_args.insert(1, "--force")
                yield "Pushing to github"
                yield from _prefix_output(run_command(push_args).stdout.splitlines())
                yield "Success!"
            else:
                diff_result.check_returncode()

# FTP and GitHub Pages logic can be filled in similarly by migrating logic from original class methods.

class FtpConnection:
    def _init_(self, target_url, credentials=None):
        credentials = credentials or {}
        url = urlsplit(target_url)
        if url.hostname is None:
            raise PublishError(f"No host name was specified in the target URL ({target_url})")
        self.con = self.make_connection()
        self.url = url
        self.username = credentials.get("username") or url.username
        self.password = credentials.get("password") or url.password
        self.log_buffer = []
        self._known_folders = set()

    @staticmethod
    def make_connection():
        from ftplib import FTP
        return FTP()

    def drain_log(self):
        log = self.log_buffer[:]
        del self.log_buffer[:]
        for chunk in log:
            for line in chunk.splitlines():
                if not isinstance(line, str):
                    line = line.decode("utf-8", "replace")
                yield line.rstrip()

    def connect(self):
        options = _parse_query(self.url.query, keep_blank_values=True)
        host = _ascii_host(self.url.hostname)
        port = self.url.port or 21
        log = self.log_buffer
        log.append("000 Connecting to server ...")
        try:
            log.append(self.con.connect(host, port))
        except Exception as e:
            log.append("000 Could not connect.")
            log.append(str(e))
            return False
        try:
            credentials = {}
            if self.username:
                credentials["user"] = self.username
            if self.password:
                credentials["passwd"] = self.password
            log.append(self.con.login(**credentials))
        except Exception as e:
            log.append("000 Could not authenticate.")
            log.append(str(e))
            return False

        passive = options.get("passive") in ("on", "yes", "true", "1", None)
        log.append("000 Using passive mode: %s" % ("yes" if passive else "no"))
        self.con.set_pasv(passive)

        try:
            log.append(self.con.cwd(self.url.path))
        except Exception as e:
            log.append(str(e))
            return False

        log.append("000 Connected!")
        return True

    def mkdir(self, path, recursive=True):
        if not isinstance(path, str):
            path = path.decode("utf-8")
        if path in self._known_folders:
            return
        dirname, _ = posixpath.split(path)
        if dirname and recursive:
            self.mkdir(dirname)
        try:
            self.con.mkd(path)
        except FTPError as e:
            if not str(e).startswith("550 "):
                self.log_buffer.append(str(e))
                return
        self._known_folders.add(path)

    def append(self, filename, data):
        if not isinstance(filename, str):
            filename = filename.decode("utf-8")
        input = io.BytesIO(data.encode("utf-8"))
        try:
            self.con.storbinary("APPE " + filename, input)
        except FTPError as e:
            self.log_buffer.append(str(e))
            return False
        return True

    def get_file(self, filename, out=None):
        if not isinstance(filename, str):
            filename = filename.decode("utf-8")
        getvalue = False
        if out is None:
            out = io.BytesIO()
            getvalue = True
        try:
            self.con.retrbinary("RETR " + filename, out.write)
        except FTPError as e:
            if not str(e).startswith("550 "):
                self.log_buffer.append(str(e))
            return None
        return out.getvalue().decode("utf-8") if getvalue else out

    def upload_file(self, filename, src, mkdir=False):
        if isinstance(src, str):
            src = io.BytesIO(src.encode("utf-8"))
        if mkdir:
            directory = posixpath.dirname(filename)
            if directory:
                self.mkdir(directory, recursive=True)
        if not isinstance(filename, str):
            filename = filename.decode("utf-8")
        try:
            self.con.storbinary("STOR " + filename, src, blocksize=32768)
        except FTPError as e:
            self.log_buffer.append(str(e))
            return False
        return True

    def rename_file(self, src, dst):
        try:
            self.con.rename(src, dst)
        except FTPError as e:
            self.log_buffer.append(str(e))
            try:
                self.con.delete(dst)
                self.con.rename(src, dst)
            except Exception as e:
                self.log_buffer.append(str(e))

    def delete_file(self, filename):
        if isinstance(filename, str):
            filename = filename.encode("utf-8")
        try:
            self.con.delete(filename)
        except Exception as e:
            self.log_buffer.append(str(e))

    def delete_folder(self, filename):
        if isinstance(filename, str):
            filename = filename.encode("utf-8")
        try:
            self.con.rmd(filename)
        except Exception as e:
            self.log_buffer.append(str(e))
        self._known_folders.discard(filename)

def publish_ftp(env, output_path, target_url, credentials, **extra):
    def iter_artifacts():
        for dirpath, dirnames, filenames in os.walk(output_path):
            dirnames[:] = [x for x in dirnames if not env.is_ignored_artifact(x)]
            for filename in filenames:
                if env.is_ignored_artifact(filename):
                    continue
                full_path = os.path.join(output_path, dirpath, filename)
                local_path = full_path[len(output_path):].lstrip(os.path.sep)
                if os.path.altsep:
                    local_path = local_path.lstrip(os.path.altsep)
                h = hashlib.sha1()
                try:
                    with open(full_path, "rb") as f:
                        while True:
                            item = f.read(4096)
                            if not item:
                                break
                            h.update(item)
                except OSError as e:
                    if e.errno != errno.ENOENT:
                        raise
                yield (local_path.replace(os.path.sep, "/"), full_path, h.hexdigest())

    def get_temp_filename(filename):
        dirname, basename = posixpath.split(filename)
        return posixpath.join(dirname, "." + basename + ".tmp")

    def read_existing_artifacts(con):
        contents = con.get_file(".lektor/listing")
        if not contents:
            return {}, set()
        duplicates = set()
        rv = {}
        for line in contents.splitlines():
            items = line.split("|")
            if len(items) == 2:
                artifact_name = items[0] if isinstance(items[0], str) else items[0].decode("utf-8")
                if artifact_name in rv:
                    duplicates.add(artifact_name)
                rv[artifact_name] = items[1]
        return rv, duplicates

    def upload_artifact(con, artifact_name, source_file, checksum):
        with open(source_file, "rb") as source:
            tmp_dst = get_temp_filename(artifact_name)
            con.log_buffer.append(f"000 Updating {artifact_name}")
            con.upload_file(tmp_dst, source, mkdir=True)
            con.rename_file(tmp_dst, artifact_name)
            con.append(".lektor/listing", f"{artifact_name}|{checksum}\n")

    def consolidate_listing(con, current_artifacts):
        server_artifacts, duplicates = read_existing_artifacts(con)
        known_folders = set(posixpath.dirname(name) for name in current_artifacts)
        for name in server_artifacts:
            if name not in current_artifacts:
                con.log_buffer.append(f"000 Deleting {name}")
                con.delete_file(name)
                folder = posixpath.dirname(name)
                if folder not in known_folders:
                    con.log_buffer.append(f"000 Deleting {folder}")
                    con.delete_folder(folder)
        if duplicates or server_artifacts != current_artifacts:
            listing = [f"{k}|{v}\n" for k, v in sorted(current_artifacts.items())]
            con.upload_file(".lektor/.listing.tmp", "".join(listing))
            con.rename_file(".lektor/.listing.tmp", ".lektor/listing")

    con = FtpConnection(target_url, credentials)
    connected = con.connect()
    yield from con.drain_log()
    if not connected:
        return
    yield "000 Reading server state ..."
    con.mkdir(".lektor")
    committed_artifacts, _ = read_existing_artifacts(con)
    yield from con.drain_log()

    yield "000 Begin sync ..."
    current_artifacts = {}
    for artifact_name, filename, checksum in iter_artifacts():
        current_artifacts[artifact_name] = checksum
        if checksum != committed_artifacts.get(artifact_name):
            upload_artifact(con, artifact_name, filename, checksum)
            yield from con.drain_log()
    yield "000 Sync done!"

    yield "000 Consolidating server state ..."
    consolidate_listing(con, current_artifacts)
    yield from con.drain_log()
    yield "000 All done!"
