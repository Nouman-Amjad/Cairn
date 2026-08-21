package cairn.tools_test

# Policy tests. These are the authorization rules stated as scenarios, and
# they run in CI: `opa test policy/`.

import data.cairn.tools
import rego.v1

engineer := {
	"sub": "alice",
	"groups": ["engineering"],
	"team": "checkout",
	"scopes": ["tools:read"],
}

sre := {
	"sub": "bob",
	"groups": ["sre"],
	"team": "checkout",
	"scopes": ["tools:read", "tools:write", "approvals:grant"],
}

admin := {
	"sub": "carol",
	"groups": ["platform-admin"],
	"team": "platform",
	"scopes": ["tools:read", "tools:write", "approvals:grant", "policy:admin"],
}

stranger := {"sub": "mallory", "groups": [], "team": null, "scopes": []}

test_engineer_can_read if {
	tools.allow with input as {"tool": "query_logs", "args": {}, "user": engineer}
}

test_stranger_can_read_nothing if {
	not tools.allow with input as {"tool": "query_logs", "args": {}, "user": stranger}
}

test_engineer_cannot_write if {
	not tools.allow with input as {
		"tool": "rollback_deploy",
		"args": {"service": "checkout-api"},
		"user": engineer,
	}
}

test_sre_can_roll_back_their_own_service if {
	tools.allow with input as {
		"tool": "rollback_deploy",
		"args": {"service": "checkout-api"},
		"user": sre,
	}
}

test_sre_cannot_roll_back_another_teams_service if {
	not tools.allow with input as {
		"tool": "rollback_deploy",
		"args": {"service": "payments-api"},
		"user": sre,
	}
}

test_platform_admin_crosses_team_boundaries if {
	tools.allow with input as {
		"tool": "rollback_deploy",
		"args": {"service": "payments-api"},
		"user": admin,
	}
}

test_unknown_tool_is_denied if {
	# The injection case: the model asks for something that does not exist.
	not tools.allow with input as {
		"tool": "exfiltrate_everything",
		"args": {},
		"user": admin,
	}
}

test_writes_always_require_approval if {
	every tool in data.tools.write {
		tools.requires_approval with input as {
			"tool": tool,
			"args": {},
			"user": admin,
		}
	}
}

test_reads_never_require_approval if {
	every tool in data.tools.readonly {
		not tools.requires_approval with input as {
			"tool": tool,
			"args": {},
			"user": admin,
		}
	}
}

test_rollback_needs_two_approvers if {
	tools.required_approvals == 2 with input as {
		"tool": "rollback_deploy",
		"args": {"service": "checkout-api"},
		"user": sre,
	}
}

test_other_writes_need_one_approver if {
	tools.required_approvals == 1 with input as {
		"tool": "create_ticket",
		"args": {},
		"user": sre,
	}
}

test_denial_carries_a_usable_reason if {
	reason := tools.reason with input as {
		"tool": "rollback_deploy",
		"args": {"service": "payments-api"},
		"user": sre,
	}
	contains(reason, "payments-api")
}
