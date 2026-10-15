# Reverse Proxy and Docker Swarm

Universal Agents can run behind a TLS-terminating reverse proxy such as
Traefik. The supported deployment model uses one public origin and routes the
website and public secure-broker forms to different internal ports.

For example:

| Public route | Internal service |
| --- | --- |
| `https://assistant.example.com/webchat` | Internal website |
| `https://assistant.example.com/docs/` | Internal website |
| `https://assistant.example.com/api/...` | Internal website |
| `https://assistant.example.com/auth/...` | Secure tool broker |
| `https://assistant.example.com/credential-collection/...` | Secure tool broker |
| `https://assistant.example.com/account-link/...` | Secure tool broker |

The reverse proxy terminates HTTPS. Universal Agents can continue using HTTP
on its private Docker overlay network.

## Configure Public and Bind Addresses

Set the public origin to the externally visible scheme and hostname. Set both
public ports to `null` so generated links do not include an internal or
host-published port:

```yaml
network:
  public_origin: "https://assistant.example.com"

interactive_apps:
  plugins:
    builtin.internal_website:
      settings:
        bind_host: "0.0.0.0"
        bind_port: 18090
        public_port: null

security_layer:
  broker_internal_base_url: "http://127.0.0.1:18600"
  broker_bind_host: "0.0.0.0"
  broker_bind_port: 18600
  broker_public_port: null
```

Use the website `bind_port` already present in your
`instance_config.yaml`. Fresh browser-onboarded instances normally use
`18090`; older or customized instances may use another port. The Traefik
service port must match this value.

When the gateway and broker run in the same Universal Agents container,
`http://127.0.0.1:18600` is the appropriate internal broker URL. If they are
deployed as separate services, use the broker's private Swarm service name
instead, such as `http://ua-broker:18600`.

The public origin can also be overridden at container launch:

```yaml
environment:
  NETWORK_PUBLIC_ORIGIN: "https://assistant.example.com"
  INTERNAL_WEBSITE_PUBLIC_PORT: ""
```

An explicitly empty `INTERNAL_WEBSITE_PUBLIC_PORT` means “use the public
origin as-is.” For an existing instance, set
`security_layer.broker_public_port: null` in `instance_config.yaml`.
`SECURE_TOOL_BROKER_PUBLIC_PORT` is exported for child-process compatibility,
but it does not override the broker public port persisted in the instance
configuration.

`network.public_origin` must be an HTTP or HTTPS origin without a path, query,
or fragment. For example, `https://assistant.example.com` is valid but
`https://assistant.example.com/ua` is not.

## Traefik Swarm Example

The following labels define two Traefik services for one Universal Agents
Swarm service. The higher-priority router sends public broker forms to port
`18600`; the lower-priority router sends all remaining requests to the
configured website bind port.

```yaml
services:
  ua:
    image: ghcr.io/enzimo/universal-agents:latest
    environment:
      NETWORK_PUBLIC_ORIGIN: "https://assistant.example.com"
      INTERNAL_WEBSITE_PUBLIC_PORT: ""
    networks:
      - proxy
    volumes:
      - type: bind
        source: /srv/universal-agents
        target: /data
    healthcheck:
      test:
        - CMD
        - curl
        - --fail
        - --silent
        - --show-error
        - --max-time
        - "5"
        - http://127.0.0.1:18090/health
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
    deploy:
      replicas: 1
      restart_policy:
        condition: any
        delay: 2s
      placement:
        constraints:
          - node.labels.ua-data == true
      labels:
        - "traefik.enable=true"
        - "traefik.swarm.network=proxy"

        - "traefik.http.routers.ua-broker.rule=Host(`assistant.example.com`) && (PathPrefix(`/auth/`) || PathPrefix(`/credential-collection/`) || PathPrefix(`/account-link/`))"
        - "traefik.http.routers.ua-broker.priority=200"
        - "traefik.http.routers.ua-broker.entrypoints=websecure"
        - "traefik.http.routers.ua-broker.tls=true"
        - "traefik.http.routers.ua-broker.service=ua-broker"
        - "traefik.http.services.ua-broker.loadbalancer.server.port=18600"

        - "traefik.http.routers.ua-web.rule=Host(`assistant.example.com`)"
        - "traefik.http.routers.ua-web.priority=10"
        - "traefik.http.routers.ua-web.entrypoints=websecure"
        - "traefik.http.routers.ua-web.tls=true"
        - "traefik.http.routers.ua-web.service=ua-web"
        - "traefik.http.services.ua-web.loadbalancer.server.port=18090"
        - "traefik.http.services.ua-web.loadbalancer.healthcheck.path=/health"

networks:
  proxy:
    external: true
```

Add the certificate resolver labels required by your Traefik installation.
For example, a deployment using a resolver named `letsencrypt` would set
`traefik.http.routers.ua-web.tls.certresolver=letsencrypt` and the equivalent
broker-router label.

Docker Swarm does not provide reliable automatic port detection to Traefik, so
each `loadbalancer.server.port` is explicit. Labels must be attached to the
Swarm service under `deploy.labels`, and Traefik and Universal Agents must
share the selected overlay network. See the
[Traefik Swarm provider documentation](https://doc.traefik.io/traefik/reference/routing-configuration/other-providers/swarm/)
for provider and network details.

Host port publishing is not required when Traefik reaches Universal Agents
over the shared overlay network. Do not publish the broker API merely to make
browser authentication work.

The example assumes `/srv/universal-agents` exists on a Swarm node labeled for
the service:

```bash
docker node update --label-add ua-data=true <node-name>
```

Change the path, node label, or volume definition to match your storage
strategy.

Both maintained Universal Agents images include `curl`. The check fails on HTTP
errors, limits each request to five seconds, and works because both browser
onboarding and the normal website runtime serve `/health` on the documented
port. If you change `interactive_apps.plugins.builtin.internal_website.settings.bind_port`, update the
health-check URL and `ua-web.loadbalancer.server.port` together.

## Public Broker Routes

The browser-facing broker surface consists of:

- `/auth/...` for interactive provider authentication, including the X
  sign-in's return page `/auth/x/callback` (register
  `https://assistant.example.com/auth/x/callback` as the X app's callback; see
  [X sign-in](hub-cli.md#x-sign-in));
- `/credential-collection/...` for secure credential submission and deletion;
- `/account-link/...` for choosing which stored credential a tool uses (an account link).

The gateway and agents call the protected broker `/v1/...` API through
`security_layer.broker_internal_base_url`. It does not need a public Traefik
route. Keeping `/v1/...` private reduces the externally reachable surface even
though the API also requires the broker token.

The website router should normally be the fallback for the hostname. Web Chat
loads root-level assets and calls `/api/...`; forwarding only `/webchat` would
load the initial page but break its scripts, styles, authentication, and chat
requests.

## First Onboarding and Existing Instances

When `/data` does not contain an onboarded `instance_config.yaml`, the
container's default `run` command automatically starts browser onboarding on
container port `18090`. With the catch-all website router above, open:

```text
https://assistant.example.com/
```

The onboarding UI uses `/`, root-level assets, and `/api/...`, so a router that
only forwards `/webchat` is not sufficient. Keep the hostname-level website
router active during onboarding.

The **Launch team** action writes the instance files to `/data` and then exits
the onboarding process normally. A Swarm restart policy with `condition: any`
starts a replacement task; the replacement sees `instance_config.yaml` and
launches the manager, gateway, Web Chat, and configured adapters. A brief
`502 Bad Gateway` while the task changes modes is normal. After the
replacement task is ready, open:

```text
https://assistant.example.com/webchat
```

Do not use `condition: on-failure` for this first-run flow: successful
onboarding exits with status zero, so an on-failure policy will not restart it.
Swarm's default restart condition is `any`, but setting it explicitly documents
and preserves the required behavior.

Keep this service at one replica during onboarding and normal single-instance
operation. The onboarding session is process-local, and multiple replicas
could independently decide that an instance has not yet been created.

Mount `/data` from durable storage that remains available when Swarm replaces
or reschedules the task. A node-local bind mount or local volume should be
paired with a placement constraint that keeps the service on the storage-owning
node; alternatively, use storage designed to be mounted from every eligible
node. Without durable shared instance state, the replacement task will start
onboarding again.

After onboarding, the website binds to
`interactive_apps.plugins.builtin.internal_website.settings.bind_port` from the generated
`instance_config.yaml`.

For a new Swarm deployment, keep that configured website bind port at `18090`
to use one Traefik service definition through onboarding and normal operation.
If an existing instance uses another bind port, change
`traefik.http.services.ua-web.loadbalancer.server.port` to match it.

After changing public URL or port settings in an existing
`instance_config.yaml`, restart the Universal Agents service so every child
process receives the same advertised URLs.

## Path Prefixes and Separate Broker Hostnames

A broker prefix such as `https://assistant.example.com/broker` is not currently
supported. The broker registers root-level `/auth`, `/credential-collection`,
`/account-link`, and `/v1` routes, and Universal Agents does not use `X-Forwarded-Prefix` to
reconstruct prefixed links. A Traefik `StripPrefix` middleware alone is
therefore insufficient.

A separate hostname such as `https://broker.example.com` can be routed to
the broker, but current generated broker links still use the shared
`network.public_origin`. Native generated-link support for separate website
and broker hostnames requires a future service-specific public-origin setting.
For now, the single-host route split shown above is the supported configuration.

If you use a separate hostname for proxy redirects, use DNS-compatible labels
such as `broker.example.com` or `assistant-broker.example.com`; do not use an
underscore in a hostname.

## Verify the Deployment

After deploying or restarting the stack:

1. Open `https://assistant.example.com/webchat` and sign in.
2. Confirm `/docs` in chat returns an HTTPS URL without `:18090`.
3. Run `/auth status` to confirm the gateway can reach the broker internally.
4. Start a supported browser authentication flow and confirm its generated URL
   begins with `https://assistant.example.com/auth/`.
5. Start a secure credential-collection flow and confirm its generated URL
   begins with `https://assistant.example.com/credential-collection/`.
6. Send `/link http:github` in chat and open the form link. Its
   `/account-link/...` URL must render the broker's account link form; form
   submission must reach the same broker.

If Web Chat renders without styles or cannot authenticate, confirm the fallback
website router also forwards root assets and `/api/...`. If broker forms return
the website's generic `404: Not Found`, confirm the broker router includes all
three browser prefixes above and has the higher priority. Deployments set up
before account links routed `/credential-binding/` instead; replace that prefix
with `/account-link/` in the existing broker rule. Apply the proxy configuration change, then reopen the pending link if it
has not expired. If applying the change restarts the broker, its in-memory
session is lost and a fresh request is required.

To check routing without opening a live form, request
`/account-link/route-check-missing`. A correctly routed nonexistent session
returns `Account link form not found or expired.` A generic `404: Not Found`
indicates that the request did not reach the broker. Keep `/v1/...` private; forwarding all broker paths is not
required.
