# Quickstart

Run Universal Agents in your browser, or install the [desktop app](https://github.com/enzimo/universal-agents-desktop/releases/latest).
For provider signup, configurable commands, and server hosting, download [the comprehensive HTML setup guide](https://github.com/enzimo/universal-agents/raw/refs/heads/main/docker-compose-setup.html) and open it in your browser.

Already have a hosted service URL? Open it in your browser and sign in; no local Docker installation is needed. Choose **Hosted server** in the guide for connection and server setup details.

## 1. Start Docker

- **macOS:** install [Homebrew](https://brew.sh), then run the commands below. Use Colima for the Linux VM.
- **Windows 11:** run `wsl --install -d Ubuntu` in administrator PowerShell and restart if prompted. Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/), select its WSL 2 backend and Linux containers, and enable integration with Ubuntu. Run the remaining commands in Ubuntu's terminal.
- **Linux:** install [Docker Engine](https://docs.docker.com/engine/install/) for your distribution and enable [Docker access for your normal user](https://docs.docker.com/engine/install/linux-postinstall/). Sign out and back in after changing group membership.

On macOS:

```sh
brew install colima docker
colima start universal-agents --cpu 2 --memory 4 --disk 40
docker context use colima-universal-agents
```

On every platform, confirm `docker info` succeeds before continuing.

## 2. Start Universal Agents

In Terminal on macOS/Linux, or Ubuntu on Windows:

```sh
curl -fsSL https://raw.githubusercontent.com/enzimo/universal-agents/v0.9.8/install.sh -o ua-install.sh
bash ua-install.sh --image ghcr.io/enzimo/universal-agents:0.9.8
```

Open **[http://127.0.0.1:18090/](http://127.0.0.1:18090/)**. The first image download can take several minutes.
Your data lives in `~/.universal-agents/data`; on Windows this is inside Ubuntu. The container includes its runtime dependencies, so you do not need to install Python, Rust, or Node.js.

<details>
<summary>Optional: GitHub authentication for restricted downloads</summary>

Public releases and images need no special access. If a download returns 404 or the image pull is denied, install [GitHub CLI](https://cli.github.com/) in the same environment as the installer, then run:

```sh
gh auth login --hostname github.com --web --scopes read:packages
gh auth token | docker login ghcr.io --username "$(gh api user --jq .login)" --password-stdin
gh api repos/enzimo/universal-agents/contents/install.sh?ref=v0.9.8 -H 'Accept: application/vnd.github.raw+json' > ua-install.sh
bash ua-install.sh --image ghcr.io/enzimo/universal-agents:0.9.8
```

Your account must have repository/package access and any required organization SSO approval. Configure a [Docker credential store](https://docs.docker.com/reference/cli/docker/login/#credential-stores) first. The HTML guide includes platform-specific helper setup. Do not paste tokens into this guide or chat.

</details>

## 3. Complete setup and chat

1. Enter your user and assistant names.
2. Choose a model provider, model, and endpoint. Enter the provider credential in onboarding and use **Test Connection**. Keep the default local storage settings for a first installation.
3. Leave optional messaging channels disabled to start with Web Chat. Save the access token shown during setup, then choose **Launch team** and sign in to Web Chat.

Send a message, or type `/tour` for a walkthrough.

To stop and start later:

```sh
docker stop universal-agents
docker start universal-agents
```

Stopping preserves your data. On macOS, start Colima again after restarting the computer. On Windows, start Docker Desktop.

For custom ports and data directories, use the HTML guide. See [model providers](user_docs_source/model-providers.md), [chat channels](user_docs_source/channels/index.md), [Docker and Compose](user_docs_source/docker.md), and [upgrade guidance](user_docs_source/configuration.md#active-runtime-state-and-admission) when you need them.
