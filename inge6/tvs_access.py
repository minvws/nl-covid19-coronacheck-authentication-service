from inge6.oidc_provider import get_oidc_provider
from os.path import exists
from typing import Optional

import requests
import base64
import uuid
import json

from jinja2 import Template

from jwkest.jwt import JWT

from fastapi.encoders import jsonable_encoder
from fastapi import Request, Response, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.security.utils import get_authorization_scheme_param

from .config import settings
from .cache import redis_cache
from .bsn_encrypt import bsn_encrypt
from .oidc_provider import get_oidc_provider
from .saml import (
    AuthNRequest, ArtifactResolveRequest, ArtifactResponseParser,
    idp_metadata, sp_metadata
)

def _login_post(relay_state):
    url = idp_metadata.get_sso()['location']

    saml_request = AuthNRequest()
    parameters = {
        'SAMLRequest': saml_request.get_base64_string().decode(),
        'RelayState': relay_state
        }

    return url, parameters

def _create_post_form(url, parameters):
    template_file = open(settings.saml.authn_request_html_template)
    template_text = template_file.read()
    template = Template(template_text)

    context = {
        'sso_url': url,
        'saml_request': parameters['SAMLRequest'],
        'relay_state': parameters['RelayState']
    }
    html = template.render(context)

    return html

def login(randstate: str, force_digid: Optional[bool] = False):
    sso_built_url_post, parameters = _login_post(relay_state=randstate)

    if settings.mock_digid.lower() == "true" and not force_digid:
        return _create_post_form(f'/digid-mock?state={randstate}', parameters)

    return _create_post_form(sso_built_url_post, parameters)

async def digid_mock(request: Request):
    body = await request.form()
    state = request.query_params['state']
    relay_state = body['RelayState']
    artifact = str(uuid.uuid4())
    http_content = f"""
    <html>
    <h1> DigiD MOCK </h1>
    <div style='font-size:36;'>
        <form method="GET" action="/digid-mock-catch">
            <label style='height:200px; width:400px' for="bsn">BSN Value:</label><br>
            <input style='height:200px; width:400px; font-size:36pt' type="text" id="bsn" value="900212640" name="bsn"><br>
            <input type="hidden" name="SAMLart" value="{artifact}">
            <input type="hidden" name="RelayState" value="{relay_state}">
            <input style='height:100px; width:400px' type="submit" value="Login">
        </form>
    </div>
    <a href='/login-digid?force_digid&state={state}' style='font-size:55; background-color:purple; display:box'>Actual DigiD</a>
    <br/>
    <a href='/digid-mock-catch?bsn=900212640&SAMLart={artifact}&RelayState={relay_state}' style='font-size:55; background-color:green; display:box'>Static BSN: 900212640</a>
    </html>
    """
    return HTMLResponse(content=http_content, status_code=200)

def digid_mock_catch(request: Request):
    bsn = request.query_params['bsn']
    relay_state = request.query_params['RelayState']
    response_uri = '/acs' + f'?SAMLart={bsn}&RelayState={relay_state}'
    return RedirectResponse(response_uri, status_code=303)

def acs(request: Request):
    state = request.query_params['RelayState']
    artifact = request.query_params['SAMLart']

    auth_req_dict = redis_cache.hget(state, 'auth_req')
    auth_req = auth_req_dict['auth_req']

    authn_response = get_oidc_provider().authorize(auth_req, 'test_user')
    response_url = authn_response.request(auth_req['redirect_uri'], False)
    code = authn_response['code']

    redis_cache.hset(code, 'arti', artifact)
    _store_code_challenge(code, auth_req_dict['code_challenge'], auth_req_dict['code_challenge_method'])
    return RedirectResponse(response_url, status_code=303)

def _store_code_challenge(code, code_challenge, code_challenge_method):
    value = {
        'code_challenge': code_challenge,
        'code_challenge_method': code_challenge_method
    }
    redis_cache.hset(code, 'cc_cm', value)

def resolve_artifact(artifact):

    if settings.mock_digid.lower() == "true":
        return bsn_encrypt._symm_encrypt_bsn(artifact)

    resolve_artifact_req = ArtifactResolveRequest(artifact).get_xml()
    url = idp_metadata.get_artifact_rs()['location']
    headers = {
        'SOAPAction' : '"https://artifact-pp2.toegang.overheid.nl/kvs/rd/resolve_artifact"',
        'content-type': 'text/xml'
        }
    resolved_artifact = requests.post(url, headers=headers, data=resolve_artifact_req, cert=('saml/certs/sp.crt', 'saml/certs/sp.key'))
    bsn = ArtifactResponseParser(resolved_artifact.text).get_bsn()
    encrypted_bsn = bsn_encrypt._symm_encrypt_bsn(bsn)
    return encrypted_bsn

def disable_access_token(b64_id_token):
    # TODO
    redis_cache.delete(b64_id_token.decode(), '')

def repack_bsn_attribute(attributes, nonce):
    decoded_json = base64.b64decode(attributes).decode()
    bsn_dict = json.loads(decoded_json)
    bsn = bsn_encrypt._symm_decrypt_bsn(bsn_dict)
    return bsn_encrypt._pub_encrypt_bsn(bsn, nonce)

def _jwt_payload(jwt: str) -> dict:
    jwt_token = JWT().unpack(jwt)
    return json.loads(jwt_token.part[1].decode())

def _validate_jwt_token(jwt):
    # TODO
    return True

async def bsn_attribute(request: Request):
    #Parse JWT token
    authorization: str = request.headers.get("Authorization")
    scheme, id_token = get_authorization_scheme_param(authorization)

    if not scheme == 'Bearer' or not _validate_jwt_token(id_token):
        raise HTTPException(status_code=401, detail="Not authorized")

    payload = _jwt_payload(id_token)
    at_hash = payload['at_hash']

    b64_id_token = base64.b64encode(id_token.encode())
    attributes = redis_cache.get(b64_id_token.decode())
    disable_access_token(b64_id_token)

    if attributes is None:
        raise HTTPException(status_code=408, detail="Resource expired.Try again after /authorize", )

    encrypted_bsn = repack_bsn_attribute(attributes, at_hash)
    jsonified_encrypted_bsn = jsonable_encoder(encrypted_bsn)
    return JSONResponse(content=jsonified_encrypted_bsn)

def metadata(request: Request):
    errors = sp_metadata.validate()

    if len(errors) == 0:
        return Response(content=sp_metadata.get_xml().decode(), media_type="application/xml")

    raise HTTPException(status_code=500, detail=', '.join(errors))
