Prefer `restart: always` for disposable Docker containers.

## Ansible Preference

Prefer simple `lookup('env/file', ...)` rather than `lookup('ansible.builtin.env/file', ...)`.
For `copy.src`, use paths relative to `files/`, e.g., `systemd/hardening.conf`.
For `template.src`, use paths relative to `templates/`, e.g., `proxy/config.yaml.j2`.
Do not use `../files/...`, `../templates/...`, or `playbook_dir` in `copy.src` and `template.src`.

For long config files in `copy` tasks, prefer files or templates rather than inline `content`.

Do not add `no_log` or use weird `owner/group/mode` just because a task handles secrets.
Control and managed node logs are trusted in this project.
Secrets can appear in them.

Never use `ansible_check_mode` in `when` checks of tasks.

Avoid implicit file creation.
If the module supports `create` and the target file is expected to exist, set `create: false`.
Otherwise, e,g, for `copy` and `template`, which do not have `create`, set `owner`, `group`, and `mode`.

Host/group variables in `inventories/group_vars/all.yaml` are treated as constants.
Do not default or assert them, e.g., `username`.

External config variables are assumed as follows to simplify checks:

- Matching types or undefined, e.g., a string variable is a string (empty or not) or undefined. a mapping variable is a mapping (`{}` or not) or undefined.
- Valid values, e.g., when a variable used as a URL segment is a string, the string has no `/`.

To remove undefined and simplify expressions, plays should default variables with `set_fact` in `pre_tasks`, or assert variables to abort with `assert` at the beginning of tasks.
Examples:

```yaml
# Default the mapping when it is undefined.
- name: Default passwords
  ansible.builtin.set_fact:
    passwords: {}
  when: passwords is undefined
```

```yaml
# Assert that password_hash exists.
- name: Check password_hash exists
  ansible.builtin.assert:
    that:
      - password_hash is defined
    fail_msg: "Set password_hash in secrets.yaml"
```

Merge these `set_fact` tasks when variables are all assigned default values.

Name these `assert` tasks with the asserted variable names or a clear category as the suffix, e.g., `Check password_hash` or `Check SSH config`.
Do not use broad names and avoid "required".
`fail_log` of the `assert` tasks should point out the file to configure the variables unless configured as host/group variables.

Put host-shared local config default and asssertion in `pre_tasks`.

To assign a variable during tasks, because ansible variables are lazily evaluated, just put the assignment in `vars`.
