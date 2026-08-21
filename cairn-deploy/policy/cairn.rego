package cairn.tools

# The authorization decision for every tool call.
#
# Evaluated per call against the *user's* claims, with no cache. If a prompt
# injection convinces the agent to call rollback_deploy, the call arrives here
# carrying the identity of whoever asked the question — and loses.

import rego.v1

default allow := false
default requires_approval := false
default required_approvals := 1

# --- reads -----------------------------------------------------------------

allow if {
	input.tool in data.tools.readonly
	some group in input.user.groups
	group in {"engineering", "sre", "platform-admin"}
}

# --- writes ----------------------------------------------------------------

allow if {
	input.tool in data.tools.write
	"tools:write" in input.user.scopes
	owns_target
}

# Ownership: you may act on services your team owns. Platform admins are
# exempt because someone has to be able to act during a cross-team incident.
owns_target if {
	"platform-admin" in input.user.groups
}

owns_target if {
	not input.args.service
}

owns_target if {
	some service in data.ownership[input.user.team]
	service == input.args.service
}

requires_approval if {
	input.tool in data.tools.write
}

required_approvals := 2 if {
	input.tool == "rollback_deploy"
}

# --- reason ----------------------------------------------------------------

reason := "ok" if allow

reason := sprintf("%s is not a known tool", [input.tool]) if {
	not input.tool in data.tools.readonly
	not input.tool in data.tools.write
}

reason := "caller lacks the write scope" if {
	input.tool in data.tools.write
	not "tools:write" in input.user.scopes
}

reason := sprintf("%s is not owned by team %s", [input.args.service, input.user.team]) if {
	input.tool in data.tools.write
	"tools:write" in input.user.scopes
	not owns_target
}

decision := {
	"allow": allow,
	"reason": reason,
	"requires_approval": requires_approval,
	"required_approvals": required_approvals,
}
