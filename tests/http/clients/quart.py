import asyncio
import contextlib
import json
import urllib.parse
from io import BytesIO
from typing import Any, Optional, AsyncGenerator, Mapping

from quart.typing import TestWebsocketConnectionProtocol
from typing_extensions import Literal

from starlette.testclient import TestClient, WebSocketTestSession

from quart import Quart
from quart import Request as QuartRequest
from quart import Response as QuartResponse
from quart.datastructures import FileStorage
from strawberry.http import GraphQLHTTPResponse
from strawberry.http.ides import GraphQL_IDE
from strawberry.quart.views import GraphQLView as BaseGraphQLView
from strawberry.types import ExecutionResult
from tests.http.context import get_context
from tests.views.schema import Query, schema

from .base import JSON, HttpClient, Response, ResultOverrideFunction, WebSocketClient, \
    Message


class GraphQLView(BaseGraphQLView[dict[str, object], object]):
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]

    result_override: ResultOverrideFunction = None

    def __init__(self, *args: Any, **kwargs: Any):
        self.result_override = kwargs.pop("result_override")
        super().__init__(*args, **kwargs)

    async def get_root_value(self, request: QuartRequest) -> Query:
        await super().get_root_value(request)  # for coverage
        return Query()

    async def get_context(
        self, request: QuartRequest, response: QuartResponse
    ) -> dict[str, object]:
        context = await super().get_context(request, response)

        return get_context(context)

    async def process_result(
        self, request: QuartRequest, result: ExecutionResult
    ) -> GraphQLHTTPResponse:
        if self.result_override:
            return self.result_override(result)

        return await super().process_result(request, result)


class QuartHttpClient(HttpClient):
    def __init__(
        self,
        graphiql: Optional[bool] = None,
        graphql_ide: Optional[GraphQL_IDE] = "graphiql",
        allow_queries_via_get: bool = True,
        result_override: ResultOverrideFunction = None,
        multipart_uploads_enabled: bool = False,
    ):
        self.app = Quart(__name__)
        self.app.debug = True

        view = GraphQLView.as_view(
            "graphql_view",
            schema=schema,
            graphiql=graphiql,
            graphql_ide=graphql_ide,
            allow_queries_via_get=allow_queries_via_get,
            result_override=result_override,
            multipart_uploads_enabled=multipart_uploads_enabled,
        )

        self.app.add_url_rule(
            "/graphql",
            view_func=view,
        )
        self.app.add_url_rule(
            '/graphql',
            view_func=view,
            methods=["GET"],
            websocket=True
        )

        self.client = TestClient(self.app)

    def create_app(self, **kwargs: Any) -> None:
        self.app = Quart(__name__)
        self.app.debug = True

        view = GraphQLView.as_view("graphql_view", schema=schema, **kwargs)

        self.app.add_url_rule(
            "/graphql",
            view_func=view,
        )
        self.app.add_url_rule(
            '/graphql',
            view_func=view,
            methods=["GET"],
            websocket=True
        )

        self.client = TestClient(self.app)


    async def _graphql_request(
        self,
        method: Literal["get", "post"],
        query: str,
        variables: Optional[dict[str, object]] = None,
        files: Optional[dict[str, BytesIO]] = None,
        headers: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> Response:
        body = self._build_body(
            query=query, variables=variables, files=files, method=method
        )

        url = "/graphql"

        if method == "get":
            body_encoded = urllib.parse.urlencode(body or {})
            url = f"{url}?{body_encoded}"
        elif body:
            if files:
                kwargs["form"] = body
                kwargs["files"] = {
                    k: FileStorage(v, filename=k) for k, v in files.items()
                }
            else:
                kwargs["data"] = json.dumps(body)

        headers = self._get_headers(method=method, headers=headers, files=files)

        return await self.request(url, method, headers=headers, **kwargs)

    async def request(
        self,
        url: str,
        method: Literal["get", "post", "patch", "put", "delete"],
        headers: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> Response:
        async with self.app.test_app() as test_app, self.app.app_context():
            client = test_app.test_client()
            response = await getattr(client, method)(url, headers=headers, **kwargs)

        return Response(
            status_code=response.status_code,
            data=(await response.data),
            headers=response.headers,
        )

    async def get(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
    ) -> Response:
        return await self.request(url, "get", headers=headers)

    async def post(
        self,
        url: str,
        data: Optional[bytes] = None,
        json: Optional[JSON] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Response:
        kwargs = {"headers": headers, "data": data, "json": json}
        return await self.request(
            url, "post", **{k: v for k, v in kwargs.items() if v is not None}
        )

    @contextlib.asynccontextmanager
    async def ws_connect(
        self,
        url: str,
        *,
        protocols: list[str],
    ) -> AsyncGenerator[WebSocketClient, None]:
        with self.client.websocket_connect(url, protocols) as ws:
            yield QuartWebSocketClient(ws)



class QuartWebSocketClient(WebSocketClient):
    def __init__(self, ws: WebSocketTestSession):
        self.ws = ws
        self._closed: bool = False
        self._close_code: Optional[int] = None
        self._close_reason: Optional[str] = None

    async def send_text(self, payload: str) -> None:
        self.ws.send_text(payload)

    async def send_json(self, payload: Mapping[str, object]) -> None:
        self.ws.send_json(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.ws.send_bytes(payload)

    async def receive(self, timeout: Optional[float] = None) -> Message:
        if self._closed:
            # if close was received via exception, fake it so that recv works
            return Message(
                type="websocket.close", data=self._close_code, extra=self._close_reason
            )
        m = self.ws.receive()
        if m["type"] == "websocket.close":
            self._closed = True
            self._close_code = m["code"]
            self._close_reason =  m.get("reason", None)
            return Message(type=m["type"], data=m["code"], extra=m.get("reason", None))
        if m["type"] == "websocket.send":
            return Message(type=m["type"], data=m["text"])
        return Message(type=m["type"], data=m["data"], extra=m["extra"])

    async def receive_json(self, timeout: Optional[float] = None) -> Any:
        m = self.ws.receive()
        assert m["type"] == "websocket.send"
        assert "text" in m
        return json.loads(m["text"])

    async def close(self) -> None:
        self.ws.close()
        self._closed = True

    @property
    def accepted_subprotocol(self) -> Optional[str]:
        return self.ws.accepted_subprotocol

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_code(self) -> int:
        assert self._close_code is not None
        return self._close_code

    @property
    def close_reason(self) -> Optional[str]:
        return self._close_reason
