"""
Defines the abstract base traits of a tox environment.
"""
import logging
import os
import re
import shutil
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union, cast

from tox.config.set_env import SetEnv
from tox.config.sets import CoreConfigSet, EnvConfigSet
from tox.execute.api import Execute, ExecuteStatus, Outcome, StdinSource
from tox.execute.request import ExecuteRequest
from tox.journal import EnvJournal
from tox.report import OutErr, ToxHandler
from tox.tox_env.errors import Recreate, Skip
from tox.tox_env.installer import Installer

from .info import Info

if TYPE_CHECKING:
    from tox.config.cli.parser import Parsed

LOGGER = logging.getLogger(__name__)


class ToxEnv(ABC):
    """A tox environment."""

    def __init__(
        self, conf: EnvConfigSet, core: CoreConfigSet, options: "Parsed", journal: EnvJournal, log_handler: ToxHandler
    ) -> None:
        """Create a new tox environment.

        :param conf: the config set to use for this environment
        :param core: the core config set
        :param options: CLI options
        :param journal: tox environment journal
        :param log_handler: handler to the tox reporting system
        """
        self.journal: EnvJournal = journal  #: handler to the tox reporting system
        self.conf: EnvConfigSet = conf  #: the config set to use for this environment
        self.core: CoreConfigSet = core  #: the core tox config set
        self.options: Parsed = options  #: CLI options
        self.log_handler: ToxHandler = log_handler  #: handler to the tox reporting system

        #: encode the run state of various methods (setup/clean/etc)
        self._run_state = {"setup": False, "clean": False, "teardown": False}
        self._paths_private: List[Path] = []  #: a property holding the PATH environment variables
        self._hidden_outcomes: Optional[List[Outcome]] = []
        self._env_vars: Optional[Dict[str, str]] = None
        self._suspended_out_err: Optional[OutErr] = None
        self._execute_statuses: Dict[int, ExecuteStatus] = {}
        self._interrupted = False

        self.register_config()
        self.cache = Info(self.env_dir)

    @staticmethod
    @abstractmethod
    def id() -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def executor(self) -> Execute:
        raise NotImplementedError

    @property
    @abstractmethod
    def installer(self) -> Installer[Any]:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.conf['env_name']})"

    def register_config(self) -> None:
        self.conf.add_constant(
            keys=["env_name", "envname"],
            desc="the name of the tox environment",
            value=self.conf.name,
        )
        self.conf.add_config(
            keys=["env_dir", "envdir"],
            of_type=Path,
            default=lambda conf, name: cast(Path, conf.core["work_dir"]) / self.name,
            desc="directory assigned to the tox environment",
        )
        self.conf.add_config(
            keys=["env_tmp_dir", "envtmpdir"],
            of_type=Path,
            default=lambda conf, name: cast(Path, conf.core["work_dir"]) / self.name / "tmp",
            desc="a folder that is always reset at the start of the run",
        )
        self.conf.default_set_env_loader = self._default_set_env
        self.conf.add_config(
            keys=["platform"],
            of_type=str,
            default="",
            desc="run on platforms that match this regular expression (empty means any platform)",
        )

        def pass_env_post_process(values: List[str]) -> List[str]:
            values.extend(self._default_pass_env())
            return sorted({k: None for k in values}.keys())

        self.conf.add_config(
            keys=["pass_env", "passenv"],
            of_type=List[str],
            default=[],
            desc="environment variables to pass on to the tox environment",
            post_process=pass_env_post_process,
        )
        self.conf.add_config(
            "parallel_show_output",
            of_type=bool,
            default=False,
            desc="if set to True the content of the output will always be shown  when running in parallel mode",
        )
        self.conf.add_config(
            "recreate",
            of_type=bool,
            default=False,
            desc="always recreate virtual environment if this option is true, otherwise leave it up to tox",
        )

    @property
    def env_dir(self) -> Path:
        """:return: the tox environments environment folder"""
        return cast(Path, self.conf["env_dir"])

    @property
    def env_tmp_dir(self) -> Path:
        """:return: the tox environments temp folder"""
        return cast(Path, self.conf["env_tmp_dir"])

    @property
    def name(self) -> str:
        return cast(str, self.conf["env_name"])

    def _default_set_env(self) -> Dict[str, str]:
        return {}

    def _default_pass_env(self) -> List[str]:
        env = [
            "https_proxy",  # HTTP proxy configuration
            "http_proxy",  # HTTP proxy configuration
            "no_proxy",  # HTTP proxy configuration
            "LANG",  # localization
            "LANGUAGE",  # localization
            "CURL_CA_BUNDLE",  # curl certificates
            "SSL_CERT_FILE",  # https certificates
            "LD_LIBRARY_PATH",  # location of libs
        ]
        if sys.stdout.isatty():  # if we're on a interactive shell pass on the TERM
            env.append("TERM")
        if sys.platform == "win32":  # pragma: win32 cover
            env.extend(
                [
                    "TEMP",  # temporary file location
                    "TMP",  # temporary file location
                    "USERPROFILE",  # needed for `os.path.expanduser()`
                    "PATHEXT",  # needed for discovering executables
                    "MSYSTEM",  # controls paths printed format
                ]
            )
        else:  # pragma: win32 no cover
            env.append("TMPDIR")  # temporary file location
        return env

    def setup(self, recreate: bool = False) -> None:
        """
        Setup the tox environment.

        :param recreate: flag to force recreation of the environment from scratch
        """
        if self._run_state["setup"] is False:  # pragma: no branch
            self._platform_check()
            recreate = recreate or cast(bool, self.conf["recreate"])
            if recreate:
                self._clean()
            try:
                self._setup_env()
                self._setup_with_env()
            except Recreate as exception:  # once we might try over
                if not recreate:  # pragma: no cover
                    logging.warning(f"recreate env because {exception.args[0]}")
                    self._clean(force=True)
                    self._setup_env()
                    self._setup_with_env()
            else:
                self._done_with_setup()
            finally:
                self._run_state.update({"setup": True, "clean": False})

    def teardown(self) -> None:
        if not self._run_state["teardown"]:
            try:
                self._teardown()
            finally:
                self._run_state.update({"teardown": True})

    def _teardown(self) -> None:
        pass

    def _platform_check(self) -> None:
        """skip env when platform does not match"""
        platform_str: str = self.conf["platform"]
        if platform_str:
            match = re.fullmatch(platform_str, self.runs_on_platform)
            if match is None:
                raise Skip(f"platform {self.runs_on_platform} does not match {platform_str}")

    @property
    @abstractmethod
    def runs_on_platform(self) -> str:
        raise NotImplementedError

    def _setup_env(self) -> None:
        """
        1. env dir exists
        2. contains a runner with the same type.
        """
        conf = {"name": self.conf.name, "type": type(self).__name__}
        with self.cache.compare(conf, ToxEnv.__name__) as (eq, old):
            if eq is False and old is not None:  # recreate if already created and not equals
                raise Recreate(f"env type changed from {old} to {conf}")
        self._handle_env_tmp_dir()

    def _setup_with_env(self) -> None:
        pass

    def _done_with_setup(self) -> None:
        """called when setup is done"""

    def _handle_env_tmp_dir(self) -> None:
        """Ensure exists and empty"""
        env_tmp_dir = self.env_tmp_dir
        if env_tmp_dir.exists() and next(env_tmp_dir.iterdir(), None) is not None:
            LOGGER.debug("clear env temp folder %s", env_tmp_dir)
            shutil.rmtree(env_tmp_dir, ignore_errors=True)
        env_tmp_dir.mkdir(parents=True, exist_ok=True)

    def _clean(self, force: bool = False) -> None:  # noqa: U100
        if self._run_state["clean"]:  # pragma: no branch
            return  # pragma: no cover
        env_dir = self.env_dir
        if env_dir.exists():
            LOGGER.warning("remove tox env folder %s", env_dir)
            shutil.rmtree(env_dir)
        self.cache.reset()
        self._run_state.update({"setup": False, "clean": True})

    @property
    def _environment_variables(self) -> Dict[str, str]:
        if self._env_vars is not None:
            return self._env_vars
        result: Dict[str, str] = {}
        pass_env: List[str] = self.conf["pass_env"]
        glob_pass_env = [re.compile(e.replace("*", ".*")) for e in pass_env if "*" in e]
        literal_pass_env = [e for e in pass_env if "*" not in e]
        for env in literal_pass_env:
            if env in os.environ:  # pragma: no branch
                result[env] = os.environ[env]
        if glob_pass_env:  # pragma: no branch
            for env, value in os.environ.items():
                if any(g.match(env) is not None for g in glob_pass_env):
                    result[env] = value
        set_env: SetEnv = self.conf["set_env"]
        # load/paths_env might trigger a load of the environment variables, set result here, returns current state
        self._env_vars = result
        # set PATH here in case setting and environment variable requires access to the environment variable PATH
        result["PATH"] = self._make_path()
        for key in set_env:
            result[key] = set_env.load(key)
        return result

    @property
    def _paths(self) -> List[Path]:
        return self._paths_private

    @_paths.setter
    def _paths(self, value: List[Path]) -> None:
        self._paths_private = value
        # also update the environment variable with the new value
        if self._env_vars is not None:  # pragma: no branch
            # remove duplicates and prepend the tox env paths
            result = self._make_path()
            self._env_vars["PATH"] = result

    def _make_path(self) -> str:
        values = dict.fromkeys(str(i) for i in self._paths)
        values.update(dict.fromkeys(os.environ.get("PATH", "").split(os.pathsep)))
        result = os.pathsep.join(values)
        return result

    def execute(
        self,
        cmd: Sequence[Union[Path, str]],
        stdin: StdinSource,
        show: Optional[bool] = None,
        cwd: Optional[Path] = None,
        run_id: str = "",
        executor: Optional[Execute] = None,
    ) -> Outcome:
        with self.execute_async(cmd, stdin, show, cwd, run_id, executor) as status:
            while status.exit_code is None:
                status.wait()
        if status.outcome is None:  # pragma: no cover # this should not happen
            raise RuntimeError  # pragma: no cover
        return status.outcome

    def interrupt(self) -> None:
        """Interrupt the execution of a tox environment."""
        logging.warning("interrupt tox environment: %s", self.conf.name)
        self._interrupted = True
        for status in list(self._execute_statuses.values()):
            status.interrupt()

    @contextmanager
    def execute_async(
        self,
        cmd: Sequence[Union[Path, str]],
        stdin: StdinSource,
        show: Optional[bool] = None,
        cwd: Optional[Path] = None,
        run_id: str = "",
        executor: Optional[Execute] = None,
    ) -> Iterator[ExecuteStatus]:
        if self._interrupted:
            raise SystemExit(-2)
        if cwd is None:
            cwd = self.core["tox_root"]
        if show is None:
            show = self.options.verbosity > 3
        request = ExecuteRequest(cmd, cwd, self._environment_variables, stdin, run_id)
        if _CWD == request.cwd:
            repr_cwd = ""
        else:
            try:
                repr_cwd = f" {_CWD.relative_to(cwd)}"
            except ValueError:
                repr_cwd = f" {cwd}"
        LOGGER.warning("%s%s> %s", run_id, repr_cwd, request.shell_cmd)
        out_err = self.log_handler.stdout, self.log_handler.stderr
        if executor is None:
            executor = self.executor
        with self._execute_call(executor, out_err, request, show) as execute_status:
            execute_id = id(execute_status)
            try:
                self._execute_statuses[execute_id] = execute_status
                yield execute_status
            finally:
                self._execute_statuses.pop(execute_id)
        if show and self._hidden_outcomes is not None:
            if execute_status.outcome is not None:  # pragma: no cover # if it gets cancelled before even starting
                self._hidden_outcomes.append(execute_status.outcome)
        if self.journal and execute_status.outcome is not None:
            self.journal.add_execute(execute_status.outcome, run_id)

    @contextmanager
    def _execute_call(
        self, executor: Execute, out_err: OutErr, request: ExecuteRequest, show: bool
    ) -> Iterator[ExecuteStatus]:
        with executor.call(
            request=request,
            show=show,
            out_err=out_err,
        ) as execute_status:
            yield execute_status

    @contextmanager
    def display_context(self, suspend: bool) -> Iterator[None]:
        with self._log_context():
            with self.log_handler.suspend_out_err(suspend, self._suspended_out_err) as out_err:
                if suspend:  # only set if suspended
                    self._suspended_out_err = out_err
                yield

    def close_and_read_out_err(self) -> Optional[Tuple[bytes, bytes]]:
        if self._suspended_out_err is None:  # pragma: no branch
            return None  # pragma: no cover
        (out, err), self._suspended_out_err = self._suspended_out_err, None
        out_b, err_b = cast(BytesIO, out.buffer).getvalue(), cast(BytesIO, err.buffer).getvalue()
        out.close()
        err.close()
        return out_b, err_b

    @contextmanager
    def _log_context(self) -> Iterator[None]:
        with self.log_handler.with_context(self.conf.name):
            yield

    @property
    def _has_display_suspended(self) -> bool:
        return self._suspended_out_err is not None


_CWD = Path.cwd()