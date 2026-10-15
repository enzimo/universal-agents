// Access model (version 2) standard policies and grant templates.
//
// Every policy and template has an @id: Cedar's text parser assigns positional
// ids otherwise, and templates are linked by id. Any evaluation error denies.

// ---------- Standard policies ----------

// Team administrators (the owner role is inside the admin role) hold full,
// delegable authority over their team's team-owned targets. Targets owned by a
// person (private folders and files, personal logins and credentials) are
// excluded: only their owner can open them to agents.
@id("std:admin")
permit (
  principal is User,
  action in [
    Action::"level.full",
    Action::"delegation.full",
    Action::"credential.use",
    Action::"credential.manage"
  ],
  resource
)
when { principal in resource.adminRole }
unless { resource has owner };

// Owners hold full, delegable authority over the targets they own.
@id("std:owner")
permit (
  principal is User,
  action in [Action::"level.full", Action::"delegation.full"],
  resource
)
when {
  (resource is Tool || resource is Operation || resource is Endpoint ||
   resource is Folder || resource is File) &&
  resource has owner && resource.owner == principal
};

// An agent may use the private folder of the person its task works for, up to
// write. Trusted code sets context.person from the task's signed context and
// marks the folder's owner; another person's folder has an owner nobody is.
// It never hands anything down: a person's folder is not grantable.
@id("std:task-person-folder")
permit (
  principal is Agent,
  action in Action::"level.write",
  resource
)
when {
  (resource is Folder || resource is File) &&
  resource has owner && context has person && resource.owner == context.person
};

@id("std:owner-credential")
permit (
  principal is User,
  action in [Action::"credential.use", Action::"credential.manage", Action::"delegation.full"],
  resource is Credential
)
when { resource has owner && resource.owner == principal };

// Nothing crosses a team boundary, whatever grants exist.
@id("std:team-boundary")
forbid (principal, action, resource)
unless { principal.team == resource.team };

// Standard guardrail: agents never hold full access to platform objects
// (settings, access, capabilities, restarts). They propose; people decide.
// It names the concrete actions: every use.* action is inside the level.full
// group, so forbidding the group would also forbid reading.
@id("guard:agents-no-platform-full")
forbid (
  principal is Agent,
  action in [Action::"use.full", Action::"delegate.full"],
  resource
)
when {
  (resource is Tool || resource is Operation || resource is Endpoint) &&
  resource.platform
};

// Tools only administrators hold (catalog owner_scope administrators), such as
// model choice: its settings apply to the whole team. A person who is not an
// administrator neither uses nor hands them on at any level, whatever grants
// exist, so an agent working for them never passes the person side either.
// Agents may still hold them for an administrator's work.
@id("guard:admin-only-tools")
forbid (
  principal is User,
  action in [Action::"level.full", Action::"delegation.full"],
  resource
)
when {
  (resource is Tool || resource is Operation || resource is Endpoint) &&
  resource.adminOnly
}
unless { principal in resource.adminRole };

// ---------- Grant templates (one link per currently valid grant) ----------
@id("tpl:grant.read")
permit (principal in ?principal, action in Action::"level.read", resource in ?resource);

@id("tpl:grant.write")
permit (principal in ?principal, action in Action::"level.write", resource in ?resource);

@id("tpl:grant.full")
permit (principal in ?principal, action in Action::"level.full", resource in ?resource);

// Linked only for delegable grants whose remaining delegation depth is >= 1.
@id("tpl:delegate.read")
permit (principal in ?principal, action in Action::"delegation.read", resource in ?resource);

@id("tpl:delegate.write")
permit (principal in ?principal, action in Action::"delegation.write", resource in ?resource);

@id("tpl:delegate.full")
permit (principal in ?principal, action in Action::"delegation.full", resource in ?resource);

@id("tpl:credential.use")
permit (principal in ?principal, action == Action::"credential.use", resource in ?resource);

// A block (restriction or emergency stop) denies whatever grants exist.
@id("tpl:block")
forbid (principal in ?principal, action, resource in ?resource);
