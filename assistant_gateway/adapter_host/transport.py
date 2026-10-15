"""Authenticated HTTP/WebSocket transport for the adapter-host application."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import ssl
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import (
    ClientResponse,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    UnixConnector,
    WSMsgType,
    web,
)

from assistant_gateway.adapter_host.service import (
    AdapterHostApplicationService,
    AdapterHostAuthenticatedPeer,
    AdapterHostServiceError,
)
from runtime_settings.interactive_apps import ADAPTER_HOST_TRANSPORT_MTLS, ADAPTER_HOST_TRANSPORT_UNIX, AdapterHostSettings
from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterAttachmentUploadAcceptedV1,
    AdapterAttachmentUploadRequestV1,
    AdapterDeliveryAcknowledgementV1,
    AdapterHeartbeatV1,
    AdapterHostContractError,
    AdapterHostMessageV1,
    AdapterProtocolErrorV1,
    AdapterRegistrationAcceptedV1,
    AdapterRegistrationV1,
    ChannelCommandV1,
    ChannelInboundMessageV1,
    ChannelRequestAcceptedV1,
    decode_adapter_host_message_v1,
    encode_adapter_host_message_v1,
)
from env_secrets import get_env_with_file_fallback
from id_generation import new_id


LOGGER = logging.getLogger(__name__)
HOST_ID_HEADER = "X-UA-Adapter-Host-Id"
AUTHORIZATION_HEADER = "Authorization"
PEER_REQUEST_KEY = web.RequestKey(
    "adapter_host_authenticated_peer", AdapterHostAuthenticatedPeer
)


def _resolve_instance_path(instance_root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (instance_root / path).resolve()


def _load_required_token(settings: AdapterHostSettings) -> str:
    token = str(get_env_with_file_fallback(settings.token_env_var) or "").strip()
    if len(token) < 32:
        raise RuntimeError(
            f"{settings.token_env_var} must contain at least 32 characters for the adapter-host transport."
        )
    return token


def _server_ssl_context(
    settings: AdapterHostSettings, instance_root: Path
) -> ssl.SSLContext:
    tls = settings.tls
    context = ssl.create_default_context(
        ssl.Purpose.CLIENT_AUTH,
        cafile=str(_resolve_instance_path(instance_root, tls.client_ca_path)),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(
        certfile=str(
            _resolve_instance_path(instance_root, tls.server_certificate_path)
        ),
        keyfile=str(_resolve_instance_path(instance_root, tls.server_private_key_path)),
    )
    return context


def _client_ssl_context(
    settings: AdapterHostSettings, instance_root: Path
) -> ssl.SSLContext:
    tls = settings.tls
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(_resolve_instance_path(instance_root, tls.server_ca_path)),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.load_cert_chain(
        certfile=str(
            _resolve_instance_path(instance_root, tls.client_certificate_path)
        ),
        keyfile=str(_resolve_instance_path(instance_root, tls.client_private_key_path)),
    )
    return context


def _certificate_identities(certificate: dict[str, Any]) -> set[str]:
    identities: set[str] = set()
    for kind, value in certificate.get("subjectAltName", ()):
        if kind in {"DNS", "URI"} and value:
            identities.add(str(value))
    for relative_name in certificate.get("subject", ()):
        for key, value in relative_name:
            if key == "commonName" and value:
                identities.add(str(value))
    return identities


def _unix_peer_uid(request: web.Request) -> int | None:
    transport = request.transport
    peer_socket = transport.get_extra_info("socket") if transport is not None else None
    if peer_socket is None:
        return None
    getpeereid = getattr(peer_socket, "getpeereid", None)
    if callable(getpeereid):
        uid, _gid = getpeereid()
        return int(uid)
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is not None:
        import struct

        raw = peer_socket.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    local_peercred = getattr(socket, "LOCAL_PEERCRED", None)
    if local_peercred is not None:
        # BSD/macOS exposes xucred through SOL_LOCAL (numeric level zero),
        # but Python does not expose SOL_LOCAL or getpeereid on every build.
        # The first two native unsigned integers are version and effective uid.
        import struct

        raw = peer_socket.getsockopt(0, local_peercred, 80)
        if len(raw) >= struct.calcsize("II"):
            _version, uid = struct.unpack_from("II", raw)
            return int(uid)
    return None


PeerAuthenticator = Callable[[web.Request], Awaitable[AdapterHostAuthenticatedPeer]]


class AdapterHostServer:
    """Private aiohttp server with UDS workload auth or mutual TLS."""

    def __init__(
        self,
        service: AdapterHostApplicationService,
        settings: AdapterHostSettings,
        *,
        instance_root: Path,
        peer_authenticator: PeerAuthenticator | None = None,
    ) -> None:
        self._service = service
        self._settings = settings
        self._instance_root = instance_root.expanduser().resolve()
        self._token = _load_required_token(settings)
        self._peer_authenticator = peer_authenticator
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        self._socket_path: Path | None = None
        self._app = web.Application(
            middlewares=[self._error_middleware, self._authentication_middleware],
            client_max_size=settings.limits.max_payload_bytes,
        )
        self._app.router.add_post("/v1/adapter-host/registrations", self._register)
        self._app.router.add_post("/v1/adapter-host/requests", self._request)
        self._app.router.add_post(
            "/v1/adapter-host/acknowledgements", self._acknowledge
        )
        self._app.router.add_post("/v1/adapter-host/heartbeats", self._heartbeat)
        self._app.router.add_get("/v1/adapter-host/events", self._events)
        self._app.router.add_get("/v1/adapter-host/events/ws", self._events_ws)
        self._app.router.add_post(
            "/v1/adapter-host/upload-tickets", self._create_upload_ticket
        )
        self._app.router.add_put(
            "/v1/adapter-host/uploads/{upload_id}", self._put_upload
        )
        self._app.router.add_get(
            "/v1/adapter-host/objects/{object_id}", self._get_object
        )
        self._app.router.add_get(
            "/v1/adapter-host/health/{plugin_id}/{adapter_instance_id}",
            self._health,
        )

    @web.middleware
    async def _error_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except AdapterHostServiceError as exc:
            LOGGER.warning(
                "Adapter-host request rejected. event=adapter_host_request_rejected "
                "error_code=%s method=%s path=%s retryable=%s",
                exc.error_code,
                request.method,
                request.path,
                exc.retryable,
            )
            return self._error_response(
                exc.error_code,
                exc.detail,
                status=exc.http_status,
                retryable=exc.retryable,
                expected_stream_position=exc.expected_stream_position,
            )
        except AdapterHostContractError as exc:
            return self._error_response(exc.error_code, exc.detail, status=400)
        except web.HTTPException:
            raise
        except Exception:
            LOGGER.exception(
                "Unhandled adapter-host transport failure. "
                "event=adapter_host_transport_failed method=%s path=%s",
                request.method,
                request.path,
            )
            return self._error_response(
                "internal_error", "adapter-host request failed", status=500
            )

    @web.middleware
    async def _authentication_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        peer = (
            await self._peer_authenticator(request)
            if self._peer_authenticator is not None
            else self._authenticate_request(request)
        )
        request[PEER_REQUEST_KEY] = peer
        return await handler(request)

    def _authenticate_request(
        self, request: web.Request
    ) -> AdapterHostAuthenticatedPeer:
        host_id = str(request.headers.get(HOST_ID_HEADER) or "").strip()
        if not host_id or host_id not in self._settings.allowed_host_ids:
            raise AdapterHostServiceError(
                "unauthenticated",
                "adapter-host identity is missing or not allowed",
                http_status=401,
            )
        authorization = str(request.headers.get(AUTHORIZATION_HEADER) or "")
        scheme, _, presented_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented_token.strip(), self._token
        ):
            raise AdapterHostServiceError(
                "unauthenticated",
                "adapter-host bearer credential is invalid",
                http_status=401,
            )

        transport = self._settings.transport
        if transport == ADAPTER_HOST_TRANSPORT_UNIX:
            if host_id != self._settings.host_id:
                raise AdapterHostServiceError(
                    "unauthenticated",
                    "Unix-socket credential is bound to a different workload identity",
                    http_status=401,
                )
            uid = _unix_peer_uid(request)
            if uid is None or uid != os.getuid():
                raise AdapterHostServiceError(
                    "unauthenticated",
                    "Unix-socket peer uid does not match the gateway workload uid",
                    http_status=401,
                )
            mechanism = "unix_peer_and_bearer"
            fingerprint_source = f"uid:{uid}:{self._token}".encode("utf-8")
        elif transport == ADAPTER_HOST_TRANSPORT_MTLS:
            request_transport = request.transport
            certificate = (
                request_transport.get_extra_info("peercert")
                if request_transport is not None
                else None
            )
            certificate_der = (
                request_transport.get_extra_info("peercert_binary")
                if request_transport is not None
                else None
            )
            if not certificate or host_id not in _certificate_identities(certificate):
                raise AdapterHostServiceError(
                    "unauthenticated",
                    "client certificate is absent or not bound to the claimed host identity",
                    http_status=401,
                )
            mechanism = "mtls_and_bearer"
            fingerprint_source = (
                certificate_der
                if isinstance(certificate_der, bytes)
                else json.dumps(certificate, sort_keys=True).encode("utf-8")
            )
        else:  # pragma: no cover - settings validation owns this invariant
            raise AdapterHostServiceError(
                "unauthenticated", "unsupported adapter-host transport", http_status=401
            )
        return AdapterHostAuthenticatedPeer(
            host_id=host_id,
            authentication_mechanism=mechanism,
            credential_fingerprint=hashlib.sha256(fingerprint_source).hexdigest(),
        )

    @staticmethod
    async def _json_payload(request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json(loads=json.loads)
        except Exception as exc:
            raise AdapterHostContractError(
                "invalid_json", "request body must contain valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise AdapterHostContractError(
                "invalid_payload", "request body must be a JSON object"
            )
        return payload

    @staticmethod
    def _peer(request: web.Request) -> AdapterHostAuthenticatedPeer:
        return request[PEER_REQUEST_KEY]

    async def _register(self, request: web.Request) -> web.Response:
        message = decode_adapter_host_message_v1(await self._json_payload(request))
        if not isinstance(message, AdapterRegistrationV1):
            raise AdapterHostContractError(
                "unexpected_message_type",
                "registration endpoint requires adapter.registration.v1",
            )
        accepted = self._service.register(self._peer(request), message)
        return web.json_response(encode_adapter_host_message_v1(accepted))

    async def _request(self, request: web.Request) -> web.Response:
        message = decode_adapter_host_message_v1(await self._json_payload(request))
        if not isinstance(message, (ChannelInboundMessageV1, ChannelCommandV1)):
            raise AdapterHostContractError(
                "unexpected_message_type",
                "request endpoint requires channel.inbound.v1 or channel.command.v1",
            )
        accepted = self._service.accept_request(self._peer(request), message)
        return web.json_response(encode_adapter_host_message_v1(accepted))

    async def _acknowledge(self, request: web.Request) -> web.Response:
        message = decode_adapter_host_message_v1(await self._json_payload(request))
        if not isinstance(message, AdapterDeliveryAcknowledgementV1):
            raise AdapterHostContractError(
                "unexpected_message_type",
                "acknowledgement endpoint requires adapter.delivery_ack.v1",
            )
        self._service.acknowledge(self._peer(request), message)
        return web.json_response({"accepted": True})

    async def _heartbeat(self, request: web.Request) -> web.Response:
        message = decode_adapter_host_message_v1(await self._json_payload(request))
        if not isinstance(message, AdapterHeartbeatV1):
            raise AdapterHostContractError(
                "unexpected_message_type",
                "heartbeat endpoint requires adapter.heartbeat.v1",
            )
        registration = self._service.heartbeat(self._peer(request), message)
        return web.json_response(
            {
                "accepted": True,
                "lease_expires_at_utc": registration.lease_expires_at_utc,
            }
        )

    @staticmethod
    def _event_query(request: web.Request) -> tuple[str, str, str, int, int, float]:
        plugin_id = str(request.query.get("plugin_id") or "").strip()
        adapter_instance_id = str(
            request.query.get("adapter_instance_id") or ""
        ).strip()
        session_epoch = str(request.query.get("session_epoch") or "").strip()
        if not plugin_id or not adapter_instance_id or not session_epoch:
            raise AdapterHostContractError(
                "invalid_payload", "event request is missing adapter stream identity"
            )
        try:
            after_position = int(request.query.get("after_position", "0"))
            limit = int(request.query.get("limit", "16"))
            wait_seconds = float(request.query.get("wait_seconds", "0"))
        except ValueError as exc:
            raise AdapterHostContractError(
                "invalid_payload", "event cursor, limit, and wait must be numeric"
            ) from exc
        if after_position < 0 or limit < 1 or limit > 1_000 or wait_seconds < 0:
            raise AdapterHostContractError(
                "invalid_payload",
                "event cursor, limit, or wait is outside its allowed range",
            )
        return (
            plugin_id,
            adapter_instance_id,
            session_epoch,
            after_position,
            limit,
            min(wait_seconds, 30.0),
        )

    async def _events(self, request: web.Request) -> web.Response:
        plugin_id, adapter_id, epoch, after, limit, wait = self._event_query(request)
        events = self._service.list_events(
            self._peer(request),
            plugin_id=plugin_id,
            adapter_instance_id=adapter_id,
            session_epoch=epoch,
            after_position=after,
            limit=limit,
        )
        if not events and wait > 0:
            await self._service.wait_for_events(wait)
            events = self._service.list_events(
                self._peer(request),
                plugin_id=plugin_id,
                adapter_instance_id=adapter_id,
                session_epoch=epoch,
                after_position=after,
                limit=limit,
            )
        return web.json_response(
            {"events": [encode_adapter_host_message_v1(event) for event in events]}
        )

    async def _events_ws(self, request: web.Request) -> web.WebSocketResponse:
        plugin_id, adapter_id, epoch, after, limit, _wait = self._event_query(request)
        websocket = web.WebSocketResponse(
            heartbeat=self._settings.limits.heartbeat_interval_seconds,
            max_msg_size=self._settings.limits.max_payload_bytes,
        )
        await websocket.prepare(request)
        peer = self._peer(request)
        cursor = after
        try:
            while not websocket.closed:
                events = self._service.list_events(
                    peer,
                    plugin_id=plugin_id,
                    adapter_instance_id=adapter_id,
                    session_epoch=epoch,
                    after_position=cursor,
                    limit=limit,
                )
                if events:
                    for event in events:
                        await websocket.send_json(encode_adapter_host_message_v1(event))
                        cursor = max(cursor, int(event.stream_position))
                    continue
                try:
                    incoming = await websocket.receive(timeout=0.1)
                except TimeoutError:
                    await self._service.wait_for_events(1.0)
                    continue
                if incoming.type == WSMsgType.TEXT:
                    message = decode_adapter_host_message_v1(json.loads(incoming.data))
                    if isinstance(message, AdapterDeliveryAcknowledgementV1):
                        self._service.acknowledge(peer, message)
                    elif isinstance(message, AdapterHeartbeatV1):
                        self._service.heartbeat(peer, message)
                    else:
                        raise AdapterHostContractError(
                            "unexpected_message_type",
                            "WebSocket client messages must be acknowledgements or heartbeats",
                        )
                elif incoming.type in {
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR,
                }:
                    break
        except AdapterHostServiceError as exc:
            await websocket.send_json(
                encode_adapter_host_message_v1(
                    self._error_message(
                        exc.error_code,
                        exc.detail,
                        retryable=exc.retryable,
                        expected_stream_position=exc.expected_stream_position,
                    )
                )
            )
            await websocket.close(code=1008, message=exc.error_code.encode("utf-8"))
        except AdapterHostContractError as exc:
            await websocket.send_json(
                encode_adapter_host_message_v1(
                    self._error_message(exc.error_code, exc.detail)
                )
            )
            await websocket.close(code=1008, message=exc.error_code.encode("utf-8"))
        return websocket

    async def _create_upload_ticket(self, request: web.Request) -> web.Response:
        message = decode_adapter_host_message_v1(await self._json_payload(request))
        if not isinstance(message, AdapterAttachmentUploadRequestV1):
            raise AdapterHostContractError(
                "unexpected_message_type",
                "upload-ticket endpoint requires adapter.attachment_upload_request.v1",
            )
        accepted = self._service.create_upload_ticket(self._peer(request), message)
        return web.json_response(encode_adapter_host_message_v1(accepted))

    async def _put_upload(self, request: web.Request) -> web.Response:
        upload_id = str(request.match_info["upload_id"] or "").strip()
        payload = await request.read()
        completed = self._service.accept_upload_bytes(
            self._peer(request), upload_id=upload_id, payload=payload
        )
        return web.json_response(
            {
                "accepted": True,
                "storage_reference": completed.storage_reference,
                "sha256": completed.sha256,
                "size_bytes": completed.size_bytes,
            }
        )

    async def _get_object(self, request: web.Request) -> web.Response:
        plugin_id = str(request.query.get("plugin_id") or "").strip()
        adapter_instance_id = str(
            request.query.get("adapter_instance_id") or ""
        ).strip()
        session_epoch = str(request.query.get("session_epoch") or "").strip()
        if not plugin_id or not adapter_instance_id or not session_epoch:
            raise AdapterHostContractError(
                "invalid_payload",
                "attachment download is missing adapter stream identity",
            )
        record, payload = self._service.read_outbound_attachment(
            self._peer(request),
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            session_epoch=session_epoch,
            object_id=str(request.match_info["object_id"]),
        )
        return web.Response(
            body=payload,
            content_type=record.content_type,
            headers={
                "X-Content-SHA256": record.sha256,
                "Content-Disposition": f'attachment; filename="{record.filename}"',
            },
        )

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(
            self._service.health(
                str(request.match_info["plugin_id"]),
                str(request.match_info["adapter_instance_id"]),
            )
        )

    @staticmethod
    def _error_message(
        error_code: str,
        detail: str,
        *,
        retryable: bool = False,
        expected_stream_position: int | None = None,
    ) -> AdapterProtocolErrorV1:
        return AdapterProtocolErrorV1(
            protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
            message_type="adapter.error.v1",
            error_id=new_id("adapter_error"),
            error_code=error_code,
            detail=detail,
            retryable=retryable,
            expected_stream_position=expected_stream_position,
        )

    @classmethod
    def _error_response(
        cls,
        error_code: str,
        detail: str,
        *,
        status: int,
        retryable: bool = False,
        expected_stream_position: int | None = None,
    ) -> web.Response:
        return web.json_response(
            encode_adapter_host_message_v1(
                cls._error_message(
                    error_code,
                    detail,
                    retryable=retryable,
                    expected_stream_position=expected_stream_position,
                )
            ),
            status=status,
        )

    async def start(self) -> None:
        if self._runner is not None:
            return
        await self._service.start()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        if self._settings.transport == ADAPTER_HOST_TRANSPORT_UNIX:
            socket_path = _resolve_instance_path(
                self._instance_root, self._settings.unix_socket_path
            )
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            if socket_path.exists():
                mode = socket_path.stat().st_mode
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError(
                        f"Refusing to replace non-socket adapter-host path {socket_path}."
                    )
                socket_path.unlink()
            self._socket_path = socket_path
            self._site = web.UnixSite(self._runner, str(socket_path))
            await self._site.start()
            os.chmod(socket_path, int(self._settings.unix_socket_mode, 8))
            LOGGER.info(
                "Adapter-host API listening on Unix socket. "
                "event=adapter_host_listening transport=unix socket_path=%s mode=%s",
                socket_path,
                self._settings.unix_socket_mode,
            )
        else:
            context = _server_ssl_context(self._settings, self._instance_root)
            self._site = web.TCPSite(
                self._runner,
                host=self._settings.bind_host,
                port=self._settings.bind_port,
                ssl_context=context,
            )
            await self._site.start()
            LOGGER.info(
                "Adapter-host API listening with mutual TLS. "
                "event=adapter_host_listening transport=mtls bind_host=%s bind_port=%d",
                self._settings.bind_host,
                self._settings.bind_port,
            )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None
        if self._socket_path is not None and self._socket_path.exists():
            if stat.S_ISSOCK(self._socket_path.stat().st_mode):
                self._socket_path.unlink()
        self._socket_path = None
        await self._service.close()


class AdapterHostTransportError(RuntimeError):
    def __init__(
        self, error_code: str, detail: str, *, retryable: bool = False
    ) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        self.retryable = retryable


class AdapterHostClient:
    """Authenticated client used by the separately supervised Python host."""

    def __init__(
        self,
        settings: AdapterHostSettings,
        *,
        instance_root: Path,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._settings = settings
        self._instance_root = instance_root.expanduser().resolve()
        self._token = _load_required_token(settings)
        self._timeout = ClientTimeout(total=request_timeout_seconds)
        self._session: ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None:
            return
        if self._settings.transport == ADAPTER_HOST_TRANSPORT_UNIX:
            connector: UnixConnector | TCPConnector = UnixConnector(
                path=str(
                    _resolve_instance_path(
                        self._instance_root, self._settings.unix_socket_path
                    )
                )
            )
        else:
            connector = TCPConnector(
                ssl=_client_ssl_context(self._settings, self._instance_root)
            )
        self._session = ClientSession(connector=connector, timeout=self._timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            HOST_ID_HEADER: self._settings.host_id,
            AUTHORIZATION_HEADER: f"Bearer {self._token}",
        }

    @property
    def _base_url(self) -> str:
        if self._settings.transport == ADAPTER_HOST_TRANSPORT_UNIX:
            return "http://localhost"
        return f"https://{self._settings.connect_host}:{self._settings.bind_port}"

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("AdapterHostClient.start() must be called first.")
        return self._session

    async def _decode_response(self, response: ClientResponse) -> dict[str, Any]:
        payload = await response.json(loads=json.loads)
        if response.status >= 400:
            message = decode_adapter_host_message_v1(payload)
            if isinstance(message, AdapterProtocolErrorV1):
                raise AdapterHostTransportError(
                    message.error_code,
                    message.detail,
                    retryable=message.retryable,
                )
            raise AdapterHostTransportError(
                "transport_error", f"adapter-host returned HTTP {response.status}"
            )
        if not isinstance(payload, dict):
            raise AdapterHostTransportError(
                "invalid_response", "adapter-host response must be a JSON object"
            )
        return payload

    async def _post_message(
        self, path: str, message: AdapterHostMessageV1
    ) -> dict[str, Any]:
        session = self._require_session()
        async with session.post(
            self._base_url + path,
            headers=self._headers,
            json=encode_adapter_host_message_v1(message),
            server_hostname=(
                self._settings.tls.server_name
                if self._settings.transport == ADAPTER_HOST_TRANSPORT_MTLS
                else None
            ),
        ) as response:
            return await self._decode_response(response)

    async def register(
        self, request: AdapterRegistrationV1
    ) -> AdapterRegistrationAcceptedV1:
        payload = await self._post_message("/v1/adapter-host/registrations", request)
        message = decode_adapter_host_message_v1(payload)
        if not isinstance(message, AdapterRegistrationAcceptedV1):
            raise AdapterHostTransportError(
                "invalid_response", "registration returned an unexpected message type"
            )
        return message

    async def submit(
        self, request: ChannelInboundMessageV1 | ChannelCommandV1
    ) -> ChannelRequestAcceptedV1:
        payload = await self._post_message("/v1/adapter-host/requests", request)
        message = decode_adapter_host_message_v1(payload)
        if not isinstance(message, ChannelRequestAcceptedV1):
            raise AdapterHostTransportError(
                "invalid_response",
                "request admission returned an unexpected message type",
            )
        return message

    async def acknowledge(
        self, acknowledgement: AdapterDeliveryAcknowledgementV1
    ) -> None:
        await self._post_message("/v1/adapter-host/acknowledgements", acknowledgement)

    async def heartbeat(self, heartbeat: AdapterHeartbeatV1) -> str:
        payload = await self._post_message("/v1/adapter-host/heartbeats", heartbeat)
        return str(payload["lease_expires_at_utc"])

    async def create_upload_ticket(
        self, request: AdapterAttachmentUploadRequestV1
    ) -> AdapterAttachmentUploadAcceptedV1:
        payload = await self._post_message("/v1/adapter-host/upload-tickets", request)
        message = decode_adapter_host_message_v1(payload)
        if not isinstance(message, AdapterAttachmentUploadAcceptedV1):
            raise AdapterHostTransportError(
                "invalid_response", "upload ticket returned an unexpected message type"
            )
        return message

    async def upload(
        self, ticket: AdapterAttachmentUploadAcceptedV1, payload: bytes
    ) -> None:
        session = self._require_session()
        async with session.put(
            self._base_url + ticket.upload_path,
            headers={**self._headers, "Content-Type": "application/octet-stream"},
            data=payload,
            server_hostname=(
                self._settings.tls.server_name
                if self._settings.transport == ADAPTER_HOST_TRANSPORT_MTLS
                else None
            ),
        ) as response:
            await self._decode_response(response)

    async def download_attachment(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        session_epoch: str,
        storage_reference: str,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> bytes:
        scheme, _, object_id = storage_reference.partition(":")
        if scheme != "gateway-object" or not object_id:
            raise AdapterHostTransportError(
                "invalid_attachment_reference",
                "outbound downloads require a gateway-object reference",
            )
        session = self._require_session()
        async with session.get(
            self._base_url + f"/v1/adapter-host/objects/{object_id}",
            headers=self._headers,
            params={
                "plugin_id": plugin_id,
                "adapter_instance_id": adapter_instance_id,
                "session_epoch": session_epoch,
            },
            server_hostname=(
                self._settings.tls.server_name
                if self._settings.transport == ADAPTER_HOST_TRANSPORT_MTLS
                else None
            ),
        ) as response:
            if response.status >= 400:
                await self._decode_response(response)
            payload = bytes(await response.read())
        if len(payload) != expected_size_bytes:
            raise AdapterHostTransportError(
                "attachment_size_mismatch",
                "downloaded attachment size does not match protocol metadata",
            )
        if hashlib.sha256(payload).hexdigest() != expected_sha256.lower():
            raise AdapterHostTransportError(
                "attachment_digest_mismatch",
                "downloaded attachment digest does not match protocol metadata",
            )
        return payload

    async def poll_events(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        session_epoch: str,
        after_position: int,
        limit: int,
        wait_seconds: float = 20.0,
    ) -> tuple[AdapterHostMessageV1, ...]:
        session = self._require_session()
        async with session.get(
            self._base_url + "/v1/adapter-host/events",
            headers=self._headers,
            params={
                "plugin_id": plugin_id,
                "adapter_instance_id": adapter_instance_id,
                "session_epoch": session_epoch,
                "after_position": str(after_position),
                "limit": str(limit),
                "wait_seconds": str(wait_seconds),
            },
            server_hostname=(
                self._settings.tls.server_name
                if self._settings.transport == ADAPTER_HOST_TRANSPORT_MTLS
                else None
            ),
        ) as response:
            payload = await self._decode_response(response)
        events = payload.get("events")
        if not isinstance(events, list):
            raise AdapterHostTransportError(
                "invalid_response", "event response must contain an events list"
            )
        return tuple(decode_adapter_host_message_v1(item) for item in events)


__all__ = [
    "AdapterHostClient",
    "AdapterHostServer",
    "AdapterHostTransportError",
    "AUTHORIZATION_HEADER",
    "HOST_ID_HEADER",
]
