import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from importlib import import_module
from mailbox import FormatError
from multiprocessing import ProcessError
from typing import Iterable


def default_workers ():
	try:
		return len(os.sched_getaffinity(0))
	except NotImplementedError as e:
		return os.cpu_count()

class DEFAULT (Enum):
	VL = logging.INFO
	OBJ_GRP = "default"
	NB_WORKERS = default_workers()
	RUN_TASK = "default"

def trans_vl (x: int) -> int:
	return 50 - x * 10

class ExecvHolder (ABC):
	@abstractmethod
	def get_argv (self) -> list:
		...
	@abstractmethod
	def get_env (self) -> dict:
		...

class ValidObject (ABC):
	@abstractmethod
	def validate (self):
		...

class GlobalContext:
	def __init__ (self, jobj: dict):
		self.nb_workers = jobj.get("nb-workers", 0)
		if "vl" in jobj:
			self.vl = trans_vl(jobj["vl"])
		else:
			self.vl = DEFAULT.VL.value
		self.modules = {}
		self.backup_backends = {
			"null": NullBackupBackend,
			"localfs": LocalfsBackupBackend
		}
		self.exec_map = {}
		self.task_map = {}
		self.l = logging.getLogger("palhm")
		self.l.setLevel(self.vl)

		if self.nb_workers == 0:
			self.nb_workers = DEFAULT.NB_WORKERS.value
		elif self.nb_workers < 0:
			self.nb_workers = None

		for m in jobj.get("modules", iter(())):
			loaded = self.modules[m] = import_module("." + m, "palhm.mod")
			intersect = set(self.backup_backends.keys()).intersection(loaded.backup_backends.keys())
			if intersect:
				raise RuntimeError("Backup Backend conflict detected. ID(s): " + intersect)
			self.backup_backends |= loaded.backup_backends

	def get_vl (self) -> int:
		return self.vl

	def get_nb_workers (self) -> int:
		return self.nb_workers

	def test_vl (self, x: int) -> bool:
		return x <= self.get_vl()

	def test_workers (self, n: int) -> bool:
		return n <= self.nb_workers if n > 0 else True

	def __str__ (self) -> str:
		return "\n".join([
			"nb_workers: " + str(self.nb_workers),
			"vl: " + str(self.vl),
			"modules: " + " ".join([ i for i in self.modules ]),
			"backup_backends: " + " ".join([ i for i in self.backup_backends.keys() ]),
			("exec_map:\n" + "\n".join([ i[0] + ": " + str(i[1]) for i in self.exec_map.items() ])).replace("\n", "\n\t"),
			("task_map:\n" + "\n".join([ (i[0] + ":\n" + str(i[1])).replace("\n", "\n\t") for i in self.task_map.items() ])).replace("\n", "\n\t")
		]).replace("\t", "  ")

class Runnable (ABC):
	@abstractmethod
	def run (self, ctx: GlobalContext):
		return self

class Exec (Runnable, ExecvHolder):
	class RE (Enum):
		EC_INC_RANGE = re.compile('''([0-9]+)(?:\s+)?-(?:\s+)?([0-9]+)''')
		EC_RANGE = re.compile('''(<|<=|>|>=|==)?(?:\s+)?([0-9]+)''')

	class DEFAULT (Enum):
		EC = range(0, 1)
		VL_STDERR = logging.ERROR
		VL_STDOUT = logging.INFO

	def parse_ec (ec: str) -> range:
		x = ec.strip()

		m = re.match(Exec.RE.EC_INC_RANGE.value, x)
		if m:
			a = int(m[1])
			b = int(m[2])
			ret = range(a, b + 1)
			if len(ret) == 0:
				raise ValueError("Invalid range: " + ec)
			return ret
		m = re.match(Exec.RE.EC_RANGE.value, x)
		if m:
			op = str(m[1]) if m[1] else "=="
			n = int(m[2])
			match op:
				case "==": return range(n, n + 1)
				case "<": return range(0, n)
				case "<=": return range(0, n + 1)
				case ">": return range(n + 1, 256)
				case ">=": return range(n, 256)
				case _: raise RuntimeError("FIXME")

		raise ValueError("Invalid value: " + ec)

	def from_conf (ctx: GlobalContext, jobj: dict):
		match jobj["type"]:
			case "exec":
				exec_id = jobj["exec-id"]
				exec = ctx.exec_map[exec_id]
				ret = exec
			case "exec-append":
				exec_id = jobj["exec-id"]
				exec = ctx.exec_map[exec_id]
				ret = exec.mkappend(jobj["argv"])
			case "exec-inline":
				ret = Exec(jobj)
			# case _:
			# 	raise RuntimeError("FIXME")

		ret.vl_stderr = jobj.get("vl-stderr", ret.vl_stderr)
		ret.vl_stdout = jobj.get("vl-stdout", ret.vl_stdout)

		return ret

	def __init__ (self, jobj: dict = None):
		if jobj is None:
			self.argv = []
			self.env = {}
			self.ec = Exec.DEFAULT.EC.value
			self.vl_stderr = Exec.DEFAULT.VL_STDERR.value
			self.vl_stdout = Exec.DEFAULT.VL_STDOUT.value
		else:
			self.argv = jobj["argv"]
			self.env = jobj.get("env") or {}
			self.ec = Exec.parse_ec(jobj.get("ec", "0"))
			self.vl_stderr = jobj.get("vl-stderr", Exec.DEFAULT.VL_STDERR.value)
			self.vl_stdout = jobj.get("vl-stdout", Exec.DEFAULT.VL_STDOUT.value)

	def mkappend (self, extra_argv: Iterable):
		ny = deepcopy(self)
		ny.argv.extend(extra_argv)
		return ny

	def run (self, ctx: GlobalContext):
		stdout = None if ctx.test_vl(self.vl_stdout) else subprocess.DEVNULL
		stderr = None if ctx.test_vl(self.vl_stderr) else subprocess.DEVNULL
		p = subprocess.run(
			self.argv,
			env = self.env,
			stdout = stdout,
			stderr = stderr)
		self.raise_oob_ec(p.returncode)

		return self

	def get_argv (self) -> list:
		return self.argv

	def get_env (self) -> dict:
		return self.env

	def test_ec (self, ec: int) -> bool:
		return ec in self.ec

	def raise_oob_ec (self, ec: int):
		if not self.test_ec(ec):
			raise ProcessError(
				str(self) + " returned " + str(ec) + " not in " + str(self.ec))

	def __str__ (self) -> str:
		return str().join(
			[ i[0] + "=\"" + i[1] + "\" " for i in self.env.items() ] +
			[ i + " " for i in self.argv ]).strip()

class BackupBackend (ABC):
	@contextmanager
	def open (self, ctx: GlobalContext):
		try:
			yield self
			self.rotate(ctx)
		except:
			self.rollback(ctx)
			raise
		finally:
			self.close(ctx)

	@abstractmethod
	def rollback (self, ctx: GlobalContext):
		...
	@abstractmethod
	def close (self, ctx: GlobalContext):
		...
	@abstractmethod
	def sink (self, ctx: GlobalContext, path: str) -> Exec:
		...
	@abstractmethod
	def rotate (self, ctx: GlobalContext):
		...
	@abstractmethod
	def _fs_usage_info (self, ctx: GlobalContext) -> Iterable[tuple[str, int]]:
		# return: copy path: du
		...
	@abstractmethod
	def _excl_fs_copies (self, ctx: GlobalContext) -> set[str]:
		...
	@abstractmethod
	def _rm_fs_recursive (self, ctx: GlobalContext, pl: Iterable[str]):
		...

	def _logger (self, ctx: GlobalContext) -> logging.Logger:
		name = "bb." + str(self)
		return ctx.l.getChild(name)

	@abstractmethod
	def _fs_quota_target (self, ctx: GlobalContext) -> tuple[Decimal, Decimal]:
		# return: nb_copies, tot_size
		...

	def _do_fs_rotate (self, ctx: GlobalContext):
		nb_copy_limit, root_size_limit = self._fs_quota_target(ctx)
		dirs = self._fs_usage_info(ctx)
		excl_copies = self._excl_fs_copies(ctx)
		l = self._logger(ctx)

		tot_size = 0
		for i in dirs:
			tot_size += i[1]

		l.debug("du: tot_size=%u, nb_copies=%u" % (tot_size, len(dirs)))
		if root_size_limit >= tot_size and nb_copy_limit >= len(dirs):
			l.debug("no action required for rotation")
			return

		size_delta = tot_size - root_size_limit
		dir_delta = len(dirs) - nb_copy_limit
		del_size = 0
		del_list = list[str]()
		while dirs and (del_size < size_delta or len(del_list) < dir_delta):
			p = dirs.pop(0)
			if p[0] in excl_copies:
				continue
			del_list.append(p[0])
			del_size += p[1]

		l.debug("deemed expired: %u copies, totalling %u bytes" %
			(len(del_list), del_size))

		self._rm_fs_recursive(ctx, del_list)

	def mkprefix_iso8601 (
		timespec: str = "seconds",
		tz: datetime.tzinfo = timezone.utc) -> str:
		return datetime.now(tz).isoformat(timespec = timespec)

class NullBackupBackend (BackupBackend):
	def __init__ (self, *args, **kwargs):
		pass

	def rollback (self, ctx: GlobalContext):
		pass

	def close (self, ctx: GlobalContext):
		pass

	def sink (self, *args, **kwargs):
		e = Exec()
		e.argv = [ "/bin/cp", "/dev/stdin", "/dev/null" ]

		return e

	def rotate (self, ctx: GlobalContext):
		pass

	def __str__ (self):
		return "null"

class LocalfsBackupBackend (BackupBackend):
	def __init__ (self, param: dict):
		self.backup_root = param["root"]
		self.mkprefix = BackupBackend.mkprefix_iso8601
		self.nb_copy_limit = Decimal(param.get("nb-copy-limit", "Infinity"))
		self.root_size_limit = Decimal(param.get("root-size-limit", "Infinity"))
		self.dmode = int(param.get("dmode", "750"), 8)
		self.fmode = int(param.get("fmode", "640"), 8)
		self.cur_backup_path = None
		self.sink_list = list[str]()

	def open (self, ctx: GlobalContext):
		self.cur_backup_path = os.sep.join([ self.backup_root, self.mkprefix() ])
		os.makedirs(self.cur_backup_path, self.dmode)

		return super().open(ctx)

	def rollback (self, ctx: GlobalContext):
		shutil.rmtree(self.cur_backup_path, ignore_errors = True)

	def close (self, ctx: GlobalContext):
		pass

	def sink (self, ctx: GlobalContext, path: str) -> Exec:
		path = os.sep.join([ self.cur_backup_path, path ])
		os.makedirs(os.path.dirname(path), self.dmode, True)
		self.sink_list.append(path)

		e = Exec()
		e.argv = [ "/bin/cp", "/dev/stdin", path ]

		return e

	def _fs_usage_info (self, ctx: GlobalContext) -> Iterable[tuple[str, int]]:
		def get_name (entry: os.DirEntry) -> str:
			return entry.name
		ret = list[tuple[str, int]]()
		dirs = LocalfsBackupBackend.get_dirs(self.backup_root)

		dirs.sort(key = get_name)
		for i in dirs:
			e = (i.path, LocalfsBackupBackend.du(i.path))
			ret.append(e)

		return ret

	def _rm_fs_recursive (self, ctx: GlobalContext, pl: Iterable[str]):
		l = self._logger(ctx)

		for i in pl:
			l.debug("rm: " + i)
			shutil.rmtree(i)

	def _fs_quota_target (self, ctx: GlobalContext) -> tuple[Decimal, Decimal]:
		return (self.nb_copy_limit, self.root_size_limit)

	def _excl_fs_copies (self, ctx: GlobalContext) -> set[str]:
		ret = set[str]()
		ret.add(self.cur_backup_path)
		return ret

	def rotate (self, ctx: GlobalContext):
		for i in self.sink_list:
			os.chmod(i, self.fmode)
		return super()._do_fs_rotate(ctx)

	def __str__ (self):
		return "localfs"

	def du (path: str) -> int:
		ret = 0
		for root, dirs, files in os.walk(path):
			for f in files:
				p = os.path.join(root, f)
				if os.path.islink(p):
					continue
				ret += os.path.getsize(p)

		return ret

	def get_dirs (path: str) -> list[os.DirEntry]:
		ret = []
		for i in os.scandir(path):
			if not i.is_symlink() and i.is_dir():
				ret.append(i)

		return ret

class BuiltinRunnable (Runnable, ValidObject):
	def __init__ (self):
		self.param = {}

def parse_signals (x: Iterable) -> set:
	ret = set()

	for sig in x:
		if sig.isnumeric():
			ret.add(signal.Signals(int(sig)))
		else:
			sig = sig.upper()
			if not sig.startswith("SIG"):
				sig = "SIG" + sig
			ret.add(signal.Signals.__members__[sig])

	return ret

class Sigmask (BuiltinRunnable):
	VALID_ACTIONS = { "block": signal.SIG_BLOCK, "unblock": signal.SIG_UNBLOCK }

	def __init__ (self, param: dict):
		self.param = param

	def validate (self):
		for i in self.param:
			self.VALID_ACTIONS[i["action"].lower()]
			parse_signals(i["sig"])

		return self

	def run (self, ctx: GlobalContext):
		for i in self.param:
			signal.pthread_sigmask(
				self.VALID_ACTIONS[i["action"].lower()],
				parse_signals(i["sig"]))

		return self

	def __str__ (self) -> str:
		return "sigmask(" + str(self.param) + ")"

BuiltinRunMap = {
	"sigmask": Sigmask
}

class Task (Runnable):
	...

class RoutineTask (Task):
	def __init__ (self, ctx: GlobalContext, jobj: dict):
		self.l = ctx.l.getChild("RoutineTask@" + jobj.get("id", hex(id(self))))
		self.routines = [] # Should hold Runnables

		for i in jobj["routine"]:
			type_str = i["type"]

			if type_str.startswith("exec"):
				r = Exec.from_conf(ctx, i)
			elif type_str == "task":
				r = ctx.task_map[i["task-id"]]
			elif type_str == "builtin":
				r = BuiltinRunMap[i["builtin-id"]](i["param"])
			else:
				raise RuntimeError("FIXME")

			self.routines.append(r)

	def run (self, ctx: GlobalContext):
		for r in self.routines:
			self.l.debug("run: " + str(r))
			p = r.run(ctx)
		return self

	def __str__ (self) -> str:
		return "\n".join([ str(i) for i in self.routines ])

class BackupObject (Runnable):
	def __init__ (
			self,
			jobj: dict,
			ctx: GlobalContext):
		self.pipeline = []
		self.path = jobj["path"]
		self.bbctx = None

		for e in jobj["pipeline"]:
			ny_exec = Exec.from_conf(ctx, e)
			self.pipeline.append(ny_exec)

	def run (self, ctx: GlobalContext):
		last_stdio = subprocess.DEVNULL # Just in case the pipeline is empty
		pmap = {}

		for eh in self.pipeline:
			p = subprocess.Popen(
				args = eh.argv,
				stdin = last_stdio,
				stdout = subprocess.PIPE,
				stderr = None if ctx.test_vl(eh.vl_stderr) else subprocess.DEVNULL,
				env = eh.env)
			pmap[eh] = p
			last_stdio = p.stdout

		sink_exec = self.bbctx.sink(ctx, self.path)
		sink_p = subprocess.Popen(
			args = sink_exec.argv,
			stdin = last_stdio,
			stdout = None if ctx.test_vl(sink_exec.vl_stdout) else subprocess.DEVNULL,
			stderr = None if ctx.test_vl(sink_exec.vl_stderr) else subprocess.DEVNULL,
			env = sink_exec.env)
		pmap[sink_exec] = sink_p

		for eh in pmap:
			p = pmap[eh]
			ec = p.wait()
			eh.raise_oob_ec(ec)

		return self

	def __str__ (self):
		return " | ".join([ str(i) for i in self.pipeline ]) + " > " + self.path

class BackupObjectGroup:
	def __init__ (self):
		self.depends = set()
		self.objects = []

class DepResolv:
	def __init__ (self):
		self.obj_dep_map = {}
		self.dep_obj_map = {}
		self.avail_q = []

	def build (og_map: dict):
		def dive (og: BackupObjectGroup, obj_set: set, recurse_path: set):
			if og in recurse_path:
				raise RecursionError("Circular reference detected.")
			recurse_path.add(og)

			obj_set.update(og.objects)
			for dep_og in og.depends:
				dive(dep_og, obj_set, recurse_path)

		ret = DepResolv()

		for gid in og_map:
			og = og_map[gid]
			if og.depends:
				dep_objs = set()
				recurse_path = set()
				for dep_og in og.depends:
					dive(dep_og, dep_objs, recurse_path)

				for obj in og.objects:
					if obj in ret.obj_dep_map:
						s = ret.obj_dep_map[obj]
					else:
						s = ret.obj_dep_map[obj] = set()
					s.update(dep_objs)
				for obj in dep_objs:
					if obj in ret.dep_obj_map:
						s = ret.dep_obj_map[obj]
					else:
						s = ret.dep_obj_map[obj] = set()
					s.update(og.objects)
			else:
				ret.avail_q.extend(og.objects)

		return ret

	def mark_fulfilled (self, obj):
		if obj in self.dep_obj_map:
			dep_s = self.dep_obj_map[obj]
			del self.dep_obj_map[obj]

			for dep in dep_s:
				obj_s = self.obj_dep_map[dep]
				obj_s.remove(obj)
				if not obj_s:
					del self.obj_dep_map[dep]
					self.avail_q.append(dep)

		return self

	def __str__ (self):
		def enclosed (self, o: BackupObject, sb: list, l: int):
			sb.append("\t" * l + o.path)
			for i in self.obj_dep_map.get(o, iter(())):
				enclosed(self, i, sb, l + 1)

		sb = []

		for i in self.obj_dep_map.keys():
			enclosed(self, i, sb, 0)

		return "\n".join(sb)

class BackupTask (Task):
	def __init__ (self, ctx: GlobalContext, jobj: dict):
		og_map = {}
		jobj_ogrps = jobj["object-groups"]
		jobj_list = jobj["objects"]
		obj_path_set = set()

		self.l = ctx.l.getChild("BackupTask@" + jobj.get("id", hex(id(self))))
		self.bb = ctx.backup_backends[jobj["backend"]](jobj.get("backend-param"))

		# check for dup ids
		for og in jobj_ogrps:
			ogid = og["id"]
			if ogid in og_map:
				raise KeyError("Duplicate object group: " + ogid)
			og_map[ogid] = BackupObjectGroup()

		# load depends
		for og in jobj_ogrps:
			ogid = og["id"]
			for depend in og.get("depends", iter(())):
				if ogid == depend:
					raise ReferenceError(
						"An object group dependent on itself: " + ogid)
				og_map[ogid].depends.add(og_map[depend])

		# implicit default
		if not DEFAULT.OBJ_GRP.value in og_map:
			og_map[DEFAULT.OBJ_GRP.value] = BackupObjectGroup()

		# load objs
		for jo in jobj_list:
			path = jo["path"]
			gid = jo.get("group", DEFAULT.OBJ_GRP.value)

			if path in obj_path_set:
				raise KeyError("Duplicate path: " + path)
			obj_path_set.add(path)
			og_map[gid].objects.append(BackupObject(jo, ctx))

		self.dep_tree = DepResolv.build(og_map)

	def run (self, ctx: GlobalContext):
		fs = set()

		with (self.bb.open(ctx) as bbctx,
			ThreadPoolExecutor(max_workers = ctx.nb_workers) as th_pool):
			while self.dep_tree.avail_q or self.dep_tree.obj_dep_map:
				if not fs and not self.dep_tree.avail_q:
					# No despatched task units, but DepResolv won't return more work
					raise RuntimeError("Invalid dependancy tree!")

				for bo in self.dep_tree.avail_q:
					bo.bbctx = bbctx
					self.l.debug("despatch: " + str(bo))
					fs.add(th_pool.submit(bo.run, ctx))
				self.dep_tree.avail_q.clear()

				f_ret = futures.wait(
					fs = fs,
					return_when = futures.FIRST_COMPLETED)
				for f in f_ret[0]:
					r = f.result()
					self.l.debug("reap: " + str(bo))
					self.dep_tree.mark_fulfilled(r)
				fs.difference_update(f_ret[0])

			for f in fs:
				self.dep_tree.mark_fulfilled(f.result())

		return self

	def __str__ (self):
		return "bb: " + str(self.bb) + "\n" + ("obj_dep_tree:\n" + str(self.dep_tree).strip()).replace("\n", "\n\t")

TaskClassMap = {
	"backup": BackupTask,
	"routine": RoutineTask
}

def merge_conf (a: dict, b: dict) -> dict:
	def chk_dup_id (key, a: dict, b: dict):
		c = set(i["id"] for i in a.get(key, iter(()))).intersection(
			set(i["id"] for i in b.get(key, iter(()))))
		return c

	# exec conflicts
	c = chk_dup_id("execs", a, b)
	if c:
		raise KeyError("Dup execs: " + c)
	# task conflicts
	c = chk_dup_id("tasks", a, b)
	if c:
		raise KeyError("Dup tasks: " + c)

	return a | b

def load_jsonc (path: str) -> dict:
	with open(path) as in_file:
		p = subprocess.run(
			[ "/bin/json_reformat" ],
			stdin = in_file,
			capture_output = True)
	if p.returncode != 0:
		raise FormatError(path)

	return json.load(io.BytesIO(p.stdout))

def load_conf (path: str, inc_set: set = set()) -> dict:
	if path in inc_set:
		raise ReferenceError("Config included multiple times: " + path)
	inc_set.add(path)

	if path.endswith(".jsonc"):
		jobj = load_jsonc(path)
	else:
		with open(path) as file:
			jobj = json.load(file)

	# TODO: do schema validation

	for i in jobj.get("include", iter(())):
		inc_conf = load_conf(i, inc_set)
		jobj = merge_conf(jobj, inc_conf)

	return jobj

def setup_conf (jobj: dict) -> GlobalContext:
	ret = GlobalContext(jobj)

	for i in jobj.get("execs", iter(())):
		ret.exec_map[i["id"]] = Exec(i)

	for i in jobj["tasks"]:
		ret.task_map[i["id"]] = TaskClassMap[i["type"]](ret, i)

	return ret
