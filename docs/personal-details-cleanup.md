# Removing personal details that were already committed

Use this when a personal detail (see `CONTRIBUTING.md`, "Personal details")
has reached a commit. How much work it takes depends on how far it got.

## Why deleting the detail is not enough

- A new commit that deletes the detail leaves it in every older commit.
- Commit metadata (author and committer name and email) cannot be changed
  without rewriting every later commit.
- On GitHub, a force-push does not remove old commits that a pull request
  still references; they stay reachable through the pull request. Deleting the
  repository is the only way to remove them.
- Release archives, container images, forks and clones made in the meantime
  keep their own copies.

## 1. Not pushed yet

Fix it locally, then run `python3 scripts/check_personal_details.py`:

- the last commit: edit the files or identity, then
  `git commit --amend --reset-author`
- older local commits: `git rebase -i <last pushed commit>` and edit each one
- the wrong identity everywhere: set `git config user.name` and
  `user.email`, then rebase with `--exec 'git commit --amend --no-edit --reset-author'`

## 2. Pushed: find out how far it went

Make a list of the real values to search for and keep it **outside the
repository** (for example `../<project>-cleanup/LOCAL-ONLY-search-terms.md`).
Never copy that list, or anything from it, into a repository, commit message,
pull request, issue or chat with an outside service. Then check:

- current files: `git grep -n -i -e <term> ...`
- history: `git log --all -p | grep -i -e <term>` and
  `git log --all --format='%an <%ae> | %cn <%ce>' | sort | uniq -c`
- release archives, container images and packages built from the repository
- open pull requests and branches, issues, wiki and repository description
- other repositories that copied files from this one

## 3. Clean the current files

On a branch: replace values with the placeholders from `CONTRIBUTING.md`, turn
copied chats, emails and logs into summaries, describe machines generically,
and delete stale notes rather than rewriting them. Run the project's tests,
open a pull request and merge it. Merge or close every other open pull
request, because unmerged branches do not move to the new repository. Make
local `main` and GitHub `main` identical (`git fetch`, then
`git rev-parse main origin/main`).

## 4. Save what the old repository holds

Write these to a folder outside the repository; record names only, never
secret values:

- unfinished work as patches: `git diff --binary HEAD > <name>.patch` for each
  worktree, `git format-patch main..<branch>` for each local branch, and
  `git stash show -p --binary stash@{N}` for each stash. These can contain old
  text, so scan them before re-applying.
- GitHub settings that are lost with the repository: secrets and variables
  (`gh secret list`, `gh variable list`), environments, labels
  (`gh label list --json name,color,description`), branch protection and
  rulesets, merge settings, Pages, webhooks, deploy keys, collaborators and
  teams, Actions permissions and self-hosted runner access, packages it
  publishes to, fine-grained tokens limited to this repository (including
  tokens stored in other repositories' secrets), description, topics and
  homepage.

## 5. Recreate the repository (the owner decides)

1. Move the old checkout to an archive folder outside any repository. Never
   push from it, and never add the new repository as its remote: one
   `git push --all` would upload the old history again.
2. Delete the repository on GitHub (Settings → Danger zone, or
   `gh repo delete <owner>/<repo>`).
3. Create an empty repository with the same name, visibility and description.
4. Build the first commit from tracked files only, with an anonymized commit
   email such as a GitHub noreply address:

   ```sh
   mkdir ../<repo>-fresh
   git -C ../<archive-folder> archive --format=tar main | tar -x -C ../<repo>-fresh
   cd ../<repo>-fresh
   git init -b main
   git config core.hooksPath .githooks
   git add -A
   git commit -m "Initial commit"
   python3 scripts/check_personal_details.py
   ```

   Also run the searches from your local list and the project's tests before
   pushing.
5. Push `main` only, then recreate the settings saved in section 4. A package the
   repository publishes to forgets the old repository's access; add the new
   one under the package's Actions access settings.
6. Publish a new release if the project has releases. Do not reuse old version
   numbers on new content.

## 6. Afterwards

- Clone the new repository, recreate worktrees and re-apply the saved patches.
- Update links, tokens and pinned references in other repositories that
  pointed at the old one. A recreated repository has a new internal ID, so
  tokens limited to the old one stop working.
- Decide whether to delete old image versions; the old packages keep the old
  files.
- Delete the archive folder once nothing in it is needed.
- Record what happened and what would have caught it earlier in the project's notes,
  without naming the detail.
