"""Admin routes for presentations."""

import json

from aiohttp import web
from aiohttp_apispec import (
    docs,
    match_info_schema,
    querystring_schema,
    request_schema,
    response_schema,
)
from marshmallow import fields
from ....connections.models.connection_record import ConnectionRecord
from ....holder.base import BaseHolder, HolderError
from .models.presentation_exchange import THCFPresentationExchange
from ....messaging.models.openapi import OpenAPISchema
from aries_cloudagent.protocols.issue_credential.v1_1.utils import retrieve_connection
from aries_cloudagent.aathcf.credentials import (
    raise_exception_invalid_state,
)
from .messages.request_proof import RequestProof
from .messages.present_proof import PresentProof
from .models.utils import retrieve_exchange
import logging
import collections

LOG = logging.getLogger(__name__).info


class PresentationRequestAPISchema(OpenAPISchema):
    connection_id = fields.Str(required=True)
    requested_attributes = fields.List(fields.Str(required=True), required=True)
    issuer_did = fields.Str(required=True)
    schema_base_dri = fields.Str(required=True)


class PresentProofAPISchema(OpenAPISchema):
    exchange_record_id = fields.Str(required=True)
    credential_id = fields.Str(required=True)


class RetrieveExchangeQuerySchema(OpenAPISchema):
    connection_id = fields.Str(required=False)
    thread_id = fields.Str(required=False)
    initiator = fields.Str(required=False)
    role = fields.Str(required=False)
    state = fields.Str(required=False)


@docs(tags=["present-proof"], summary="Sends a proof presentation")
@request_schema(PresentationRequestAPISchema())
async def request_presentation_api(request: web.BaseRequest):
    """Request handler for sending a presentation."""
    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]
    body = await request.json()

    connection_id = body.get("connection_id")
    await retrieve_connection(context, connection_id)  # throw exception if not found

    presentation_request = {
        "requested_attributes": body.get("requested_attributes"),
        "issuer_did": body.get("issuer_did"),
        "schema_base_dri": body.get("schema_base_dri"),
    }

    message = RequestProof(presentation_request=presentation_request)
    await outbound_handler(message, connection_id=connection_id)

    exchange_record = THCFPresentationExchange(
        connection_id=connection_id,
        thread_id=message._thread_id,
        initiator=THCFPresentationExchange.INITIATOR_SELF,
        role=THCFPresentationExchange.ROLE_VERIFIER,
        state=THCFPresentationExchange.STATE_REQUEST_SENT,
        presentation_request=presentation_request,
    )

    LOG("exchange_record %s", exchange_record)
    await exchange_record.save(context)
    return web.json_response({"thread_id": exchange_record.thread_id})


@docs(tags=["present-proof"], summary="Send a credential presentation")
@request_schema(PresentProofAPISchema())
async def present_proof_api(request: web.BaseRequest):
    """
    Allows to respond to an already existing exchange with a proof presentation.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange details

    """
    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    body = await request.json()
    exchange_record_id = body.get("exchange_record_id")
    credential_id = body.get("credential_id")

    exchange_record = await retrieve_exchange(
        context, exchange_record_id, web.HTTPNotFound
    )

    raise_exception_invalid_state(
        exchange_record,
        THCFPresentationExchange.STATE_REQUEST_RECEIVED,
        THCFPresentationExchange.ROLE_PROVER,
        web.HTTPBadRequest,
    )

    connection_record: ConnectionRecord = await retrieve_connection(
        context, exchange_record.connection_id
    )

    try:
        holder: BaseHolder = await context.inject(BaseHolder)
        requested_credentials = {"credential_id": credential_id}
        presentation = await holder.create_presentation(
            presentation_request=exchange_record.presentation_request,
            requested_credentials=requested_credentials,
            schemas={},
            credential_definitions={},
        )
    except HolderError as err:
        raise web.HTTPInternalServerError(reason=err.roll_up)

    message = PresentProof(credential_presentation=presentation)
    message.assign_thread_id(exchange_record.thread_id)
    await outbound_handler(message, connection_id=connection_record.connection_id)

    exchange_record.state = exchange_record.STATE_PRESENTATION_SENT
    exchange_record.presentation = json.loads(
        presentation, object_pairs_hook=collections.OrderedDict
    )
    await exchange_record.save(context)

    return web.json_response("success, proof sent and exchange updated")


@docs(tags=["present-proof"], summary="retrieve exchange record")
@querystring_schema(RetrieveExchangeQuerySchema())
async def retrieve_credential_exchange_api(request: web.BaseRequest):
    context = request.app["request_context"]

    records = await THCFPresentationExchange.query(context, tag_filter=request.query)

    result = []
    for i in records:
        result.append(i.serialize())

    return web.json_response(result)


async def register(app: web.Application):
    """Register routes."""

    app.add_routes(
        [
            web.post(
                "/present-proof/request",
                request_presentation_api,
            ),
            web.post(
                "/present-proof/present",
                present_proof_api,
            ),
            web.get(
                "/present-proof/exchange/record",
                retrieve_credential_exchange_api,
                allow_head=False,
            ),
        ]
    )


def post_process_routes(app: web.Application):
    """Amend swagger API."""

    # Add top-level tags description
    if "tags" not in app._state["swagger_dict"]:
        app._state["swagger_dict"]["tags"] = []
    app._state["swagger_dict"]["tags"].append(
        {
            "name": "present-proof",
            "description": "Proof presentation",
            "externalDocs": {"description": "Specification"},
        }
    )