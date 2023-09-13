from __future__ import annotations

import logging
from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    TypedDict,
    Union,
)

from fastapi import HTTPException, status
from pydantic import ValidationError
from starlette.requests import Request

from fastapi_jsonapi import RoutersJSONAPI
from fastapi_jsonapi.atomic.prepared_atomic_operation import LocalIdsType, OperationBase
from fastapi_jsonapi.atomic.schemas import AtomicOperationRequest, AtomicResultResponse
from fastapi_jsonapi.utils.dependency_helper import DependencyHelper
from fastapi_jsonapi.views.utils import HTTPMethodConfig

if TYPE_CHECKING:
    from fastapi_jsonapi.data_layers.base import BaseDataLayer

log = logging.getLogger(__name__)
AtomicResponseDict = TypedDict("AtomicResponseDict", {"atomic:results": List[Any]})


class AtomicViewHandler:
    def __init__(
        self,
        request: Request,
        operations_request: AtomicOperationRequest,
    ):
        self.request = request
        self.operations_request = operations_request

    async def handle_view_dependencies(
        self,
        jsonapi: RoutersJSONAPI,
    ) -> Dict[str, Any]:
        method_config: HTTPMethodConfig = jsonapi.get_method_config_for_create()

        def handle_dependencies(**dep_kwargs):
            return dep_kwargs

        handle_dependencies.__signature__ = jsonapi.prepare_dependencies_handler_signature(
            custom_handler=handle_dependencies,
            method_config=method_config,
        )

        dependencies_result: Dict[str, Any] = await DependencyHelper(request=self.request).run(handle_dependencies)
        return dependencies_result

    async def prepare_operations(self) -> List[OperationBase]:
        prepared_operations: List[OperationBase] = []

        for operation in self.operations_request.operations:
            operation_type = operation.ref and operation.ref.type or operation.data and operation.data.type
            assert operation_type
            jsonapi = RoutersJSONAPI.all_jsonapi_routers[operation_type]

            dependencies_result: Dict[str, Any] = await self.handle_view_dependencies(
                jsonapi=jsonapi,
            )
            one_operation = OperationBase.prepare(
                action=operation.op,
                request=self.request,
                jsonapi=jsonapi,
                ref=operation.ref,
                data=operation.data,
                data_layer_view_dependencies=dependencies_result,
            )
            prepared_operations.append(one_operation)

        return prepared_operations

    async def handle(self) -> Union[AtomicResponseDict, AtomicResultResponse, None]:
        prepared_operations = await self.prepare_operations()
        results = []

        # TODO: try/except, catch schema ValidationError

        only_empty_responses = True
        local_ids_cache: LocalIdsType = defaultdict(dict)
        success = True
        previous_dl: Optional[BaseDataLayer] = None
        for idx, operation in enumerate(prepared_operations, start=1):
            dl: BaseDataLayer = await operation.get_data_layer()
            await dl.atomic_start(previous_dl=previous_dl)
            previous_dl = dl
            try:
                operation.update_relationships_with_lid(local_ids=local_ids_cache)
                response = await operation.handle(dl=dl)
            except (ValidationError, ValueError) as ex:
                log.exception(
                    "Validation error on atomic action ref=%s, data=%s",
                    operation.ref,
                    operation.data,
                )
                errors_details = {
                    "message": f"Validation error on operation #{idx}",
                    "ref": operation.ref,
                    "data": operation.data.dict(),
                }
                if isinstance(ex, ValidationError):
                    errors_details.update(errors=ex.errors())
                elif isinstance(ex, ValueError):
                    errors_details.update(error=str(ex))
                else:
                    raise
                # TODO: json:api exception
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=errors_details,
                )
            # response.data.id
            if not response:
                # https://jsonapi.org/ext/atomic/#result-objects
                # An empty result object ({}) is acceptable
                # for operations that are not required to return data.
                results.append({})
                continue
            only_empty_responses = False
            results.append({"data": response.data})
            if operation.data.lid and response.data:
                local_ids_cache[operation.data.type][operation.data.lid] = response.data.id

        if previous_dl:
            await previous_dl.atomic_end(success=success)

        if not only_empty_responses:
            return {"atomic:results": results}

        """
        if all results are empty,
        the server MAY respond with 204 No Content and no document.
        """
