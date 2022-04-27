#!/bin/bash
set -e
. "$( dirname -- "${BASH_SOURCE[0]}" )"/common.sh

do_query () {
	# dig returns 0 upon successful reception and parse of the response message.
	# All the other exit codes other than 0 will cause the script to terminate
	# as a result of set -e. +short option makes dig return the values of RR.
	# We assume that a status code has returned when dig produces no output with
	# the option. Caution must be taken in this approach as zones with no
	# record will also return nothing with the status code zero.
	dig +short +dnssec ANY "$TARGET" > "$tmpf"
	if [ ! -s "$tmpf" ]; then
		palhm_die \
			"The nameserver returned no RR!
DNSSEC verification probably failed."
	fi
}

if [ "$#" -lt 1 ]; then
	cat >&2 << EOF
The Periodic Automatic Linux Host Maintenance (PALHM) DNSSEC check
Usage: $0 <record name>

The zone must contain at least one resource record set. The nameservers
configured for the host must support DNSSEC validation.

To test your host configuration, running
  \$ $0 dnssec-failed.org
should produce error messages.
EOF
	exit 2
fi

declare TARGET="$1"
declare tmpf="$(mktemp --tmpdir "palhm-dnssec.XXXXXXXXXX")"

do_query & set +e
wait -f "$!"
ec="$?"
rm "$tmpf"

exit "$ec"
