#!/usr/bin/env python3
import logging
import sys
from abc import ABC, abstractmethod
from getopt import getopt

import palhm


class ProgConf:
	conf = "/etc/palhm/palhm.conf"
	cmd = None
	override_vl = None
	ctx = None

	def alloc_ctx ():
		ProgConf.ctx = palhm.setup_conf(palhm.load_conf(ProgConf.conf))
		if not ProgConf.override_vl is None:
			ProgConf.ctx.l.setLevel(ProgConf.override_vl)

def err_unknown_cmd ():
	sys.stderr.write("Unknown command. Run '" + sys.argv[0] + " help' for usage.\n")
	exit(2)

class Cmd (ABC):
	@abstractmethod
	def do_cmd (self):
		...

class ConfigCmd (Cmd):
	def __init__ (self, *args, **kwargs):
		pass

	def do_cmd (self):
		ProgConf.alloc_ctx()
		print(ProgConf.ctx)
		return 0

	def print_help ():
		print(
"Usage: " + sys.argv[0] + " config" + '''
Load and parse config. Print the structure to stdout.''')

class RunCmd (Cmd):
	def __init__ (self, optlist, args):
		self.optlist = optlist
		self.args = args

	def do_cmd (self):
		ProgConf.alloc_ctx()

		if self.args:
			task = self.args[0]
		else:
			task = palhm.DEFAULT.RUN_TASK.value

		ProgConf.ctx.task_map[task].run(ProgConf.ctx)

		return 0

	def print_help ():
		print(
"Usage: " + sys.argv[0] + " run [TASK]" + '''
Run a task in config. Run the "''' + palhm.DEFAULT.RUN_TASK.value +
'''" task if [TASK] is not specified.''')

class HelpCmd (Cmd):
	def __init__ (self, optlist, args):
		self.optlist = optlist
		self.args = args

	def do_cmd (self):
		if len(self.args) >= 2:
			if not args[0] in CmdMap:
				err_unknown_cmd()
			else:
				CmdMap[self.args[0]].print_help()
		else:
			HelpCmd.print_help()

		return 0

	def print_help ():
		print(
"Usage: " + sys.argv[0] + " [options] CMD [command options ...]" + '''
Options:
  -q       Set the verbosity level to 0(FATAL error only). Overrides config
  -v       Increase the verbosity level by 1. Overrides config
  -f FILE  Load config from FILE instead of the hard-coded default
Config: ''' + ProgConf.conf + '''
Commands:
  run         run a task
  config      load config and print the contents
  help [CMD]  print this message and exit normally if [CMD] is not specified.
              Print usage of [CMD] otherwise''')

		return 0

CmdMap = {
	"config": ConfigCmd,
	"run": RunCmd,
	"help": HelpCmd
}

optlist, args = getopt(sys.argv[1:], "qvf:")
optkset = set()
for p in optlist:
	optkset.add(p[0])

if "-v" in optkset and "-q" in optkset:
	sys.stderr.write("Options -v and -q cannot not used together.\n")
	exit(2)

if not args or not args[0] in CmdMap:
	err_unknown_cmd()

for p in optlist:
	match p[0]:
		case "-q": ProgConf.override_vl = logging.ERROR
		case "-v":
			if ProgConf.override_vl is None:
				ProgConf.override_vl = palhm.DEFAULT.VL.value - 10
			else:
				ProgConf.override_vl -= 10
		case "-f": ProgConf.conf = p[1]

logging.basicConfig(format = "%(name)s %(message)s")

ProgConf.cmd = CmdMap[args[0]](optlist, args)
del args[0]
exit(ProgConf.cmd.do_cmd())
