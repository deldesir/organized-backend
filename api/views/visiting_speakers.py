"""Visiting-speakers cross-congregation sharing views.

Mirrors the sws2apps-api visiting-speakers access flow. On a single
self-hosted box there is normally only ONE congregation, so there are no
OTHER congregations to share with or request from. These endpoints are
therefore stub-noops: they authenticate via the session cookie (request.cong_user,
exactly like MasterKeyView), accept requests gracefully, and return
sensible EMPTY but correctly-typed payloads so the client never crashes on a
missing/mis-typed field.

In a full federation build the approved-access list rides in
Congregation.outgoing_speakers and pending requests in
Congregation.incoming_reports; here we just return correctly-typed empties.

Response-shape note (traced from src/services/api/visitingSpeakers.ts):
the frontend api fns wrap the raw HTTP body — `result: data` or `data` —
so the BODY we return here is the inner object/array the hooks read, not a
second-level {status, result} envelope (status comes from the HTTP status).
"""

import json
import logging

from django.http import JsonResponse
from django.views import View

logger = logging.getLogger('organized.visiting_speakers')


class ApprovedAccessView(View):
    """GET /api/v3/congregations/meeting/<id>/visiting-speakers/access

    Congregations approved to access OUR visiting speakers.
    Body becomes `result` in apiGetApprovedVisitingSpeakersAccess; the hook
    useCongregationsAccess reads result.congregations (CongregationRequestType[]).
    Self-hosted single-congregation: {congregations: []} correctly typed.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        return JsonResponse({'congregations': []})


class FindCongregationsView(View):
    """GET /api/v3/congregations/meeting/<id>/visiting-speakers/congregations?name=<query>

    Search OTHER congregations to request speaker access from.
    apiFindCongregationSpeakers returns `data` (the raw body) and useOnline
    calls setOptions(data) expecting an ARRAY (IncomingCongregationResponseType[]).
    Self-hosted: no other congregations -> return [] directly.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        # name = request.GET.get('name', '')  # no federation peers to search
        return JsonResponse([], safe=False)


class PendingAccessView(View):
    """GET /api/v3/congregations/meeting/<id>/visiting-speakers/pending-access

    Pending requests from OTHER congregations wanting access to our speakers.
    Body becomes `result`; mirrors the approved-access shape plus
    pending_speakers_requests. Self-hosted: both empty, correctly typed.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        return JsonResponse({
            'congregations': [],
            'pending_speakers_requests': [],
        })


class RequestAccessView(View):
    """POST /api/v3/congregations/meeting/<id>/visiting-speakers/request

    A congregation requests access to another congregation's visiting speakers.
    apiRequestAccessCongregationSpeakers returns `data` (the raw body);
    useCongregationAdd checks status===200 and reads data.message on error.
    Body: {cong_id, request_id, key}. Self-hosted with no federation peer:
    accept gracefully and return {message: 'REQUEST_SENT'}.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        return JsonResponse({'message': 'REQUEST_SENT'})


class ApproveRequestView(View):
    """POST /api/v3/congregations/meeting/<id>/visiting-speakers/request/approve

    Admin approves a pending speaker-access request. Body becomes `result`;
    useSpeakerAccessRequest reads result.congregations (and result.message on
    error). Body: {request_id, key}. Self-hosted stub: {congregations: []}.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        return JsonResponse({'congregations': []})


class RejectRequestView(View):
    """POST /api/v3/congregations/meeting/<id>/visiting-speakers/request/reject

    Admin rejects a pending request (or revokes an approved one). Body becomes
    `result`; useSpeakerAccessRequest / useCongregationsAccess read
    result.congregations. Body: {request_id}. Self-hosted stub: {congregations: []}.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        return JsonResponse({'congregations': []})
