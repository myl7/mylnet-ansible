Never use any path that is outside this project in the files.
Example: Changing ./.ssh/id_rsa to ~/.ssh/id_rsa is wrong.

## Ansible Preference

Prefer simple `lookup('env/file', ...)` rather than `lookup('ansible.builtin.env/file', ...)`.
Prefer simple relative path from the playbook rather than `playbook_dir ~ '/.../...'`.
Never use `ansible_check_mode` in `when` checks of tasks.
