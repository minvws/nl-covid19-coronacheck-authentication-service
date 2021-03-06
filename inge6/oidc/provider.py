# Copyright (c) 2020-2021 De Staat der Nederlanden, Ministerie van Volksgezondheid, Welzijn en Sport.
#
# Licensed under the EUROPEAN UNION PUBLIC LICENCE v. 1.2
#
# SPDX-License-Identifier: EUPL-1.2
#
from typing import Any
import json

from jwkest.jwk import RSAKey, rsa_load

from pyop.storage import RedisWrapper
from pyop.authz_state import AuthorizationState
from pyop.provider import Provider as PyopProvider
from pyop.subject_identifier import HashBasedSubjectIdentifierFactory
from pyop.userinfo import Userinfo

from ..config import settings
from ..cache import get_redis_client

REDIS_TTL = int(settings.redis.object_ttl)

# pylint: disable=too-few-public-methods
class Provider:

    def __init__(self, app) -> None:
        issuer = settings.issuer
        authentication_endpoint = app.url_path_for('authorize')
        jwks_uri = app.url_path_for('jwks_uri')
        token_endpoint = app.url_path_for('token_endpoint')

        configuration_information = {
            'issuer': issuer,
            'authorization_endpoint': issuer + authentication_endpoint,
            'jwks_uri': issuer + jwks_uri,
            'token_endpoint': issuer + token_endpoint,
            'scopes_supported': ['openid'],
            'response_types_supported': ['code'],
            'response_modes_supported': ['query'],
            'grant_types_supported': ['authorization_code'],
            'subject_types_supported': ['pairwise'],
            'token_endpoint_auth_methods_supported': ['none'],
            'claims_parameter_supported': True
        }

        userinfo_db = Userinfo({'test_client': {'test': 'test_client'}})
        with open(settings.oidc.clients_file) as clients_file:
            clients = json.load(clients_file)

        signing_key = RSAKey(key=rsa_load(settings.oidc.rsa_private_key), alg='RS256', )

        authorization_code_db = RedisWrapper(collection=settings.redis.code_namespace, redis=get_redis_client(), ttl=REDIS_TTL)
        access_token_db = RedisWrapper(collection=settings.redis.token_namespace, redis=get_redis_client(), ttl=REDIS_TTL)
        refresh_token_db = RedisWrapper(collection=settings.redis.refresh_token_namespace, redis=get_redis_client(), ttl=REDIS_TTL)
        subject_identifier_db = RedisWrapper(collection=settings.redis.sub_id_namespace, redis=get_redis_client(), ttl=REDIS_TTL)

        authz_state = AuthorizationState(
            HashBasedSubjectIdentifierFactory(settings.oidc.subject_id_hash_salt),
            authorization_code_db=authorization_code_db,
            access_token_db=access_token_db,
            refresh_token_db=refresh_token_db,
            subject_identifier_db=subject_identifier_db
        )

        self.provider = PyopProvider(signing_key, configuration_information,
                            authz_state, clients, userinfo_db, id_token_lifetime= int(settings.oidc.id_token_lifetime))

        with open('secrets/public.pem') as rsa_priv_key:
            self.key = rsa_priv_key.read()

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.provider, name):
            return getattr(self.provider, name)

        raise AttributeError("Attribute {} not found".format(name))
