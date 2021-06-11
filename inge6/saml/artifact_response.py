# pylint: disable=c-extension-no-member
from typing import Text

import base64
import re
import json
import logging

from typing import List

from Crypto.Cipher import AES
from lxml import etree

from onelogin.saml2.utils import OneLogin_Saml2_Utils

from ..config import settings
from .utils import from_settings, has_valid_signatures, remove_padding
from .constants import NAMESPACES
from .exceptions import UserNotAuthenticated, ValidationError
from .provider import Provider as SAMLProvider

SUCCESS = "success"

PRIV_KEY_PATH = settings.saml.key_path
CAMEL_TO_SNAKE_RE = re.compile(r'(?<!^)(?=[A-Z])')


def verify_signatures(tree, cert_data):
    root, valid = has_valid_signatures(tree, cert_data=cert_data)
    if not valid:
        raise ValidationError("Invalid signatures")

    return root


class ArtifactResponse:

    def __init__(self, artifact_tree, provider: SAMLProvider, is_verified: bool = True) -> None:
        self.provider = provider
        self.is_verifeid = is_verified

        self._root = artifact_tree
        self._response = None
        self._response_status = None
        self._saml_status_code = None
        self._status = None
        self._response_audience_restriction = None
        self._response_assertion = None
        self._advice_assertion = None
        self._assertion_attribute_enc_key = None
        self._assertion_attribute_enc_data = None
        self._issuer = None
        self._response_issuer = None
        self._assertion_issuer = None
        self._advice_assertion_issuer = None

        self.validate()

    @classmethod
    def from_string(cls, xml_response: str, provider: SAMLProvider, insecure=False):
        artifact_response_tree = etree.fromstring(xml_response).getroottree().getroot()
        return cls.parse(artifact_response_tree, provider, insecure)

    @classmethod
    def parse(cls, artifact_response_tree, provider: SAMLProvider, insecure=False):
        unverified_tree = artifact_response_tree.find('.//samlp:ArtifactResponse', NAMESPACES)
        if insecure:
            return cls(unverified_tree, provider, False)

        verified_tree = verify_signatures(artifact_response_tree, provider.idp_metadata.get_cert_pem_data())
        return cls(verified_tree, provider, True)

    @property
    def root(self):
        return self._root

    @property
    def response(self):
        if self._response is not None:
            return self._response

        self._response = self.root.find('.//samlp:Response', NAMESPACES)
        return self._response

    @property
    def response_status(self):
        if self._response_status is not None:
            return self._response_status

        self._response_status = self.response.find('./samlp:Status', NAMESPACES)
        return self._response_status

    @property
    def saml_status_code(self) -> str:
        if self._saml_status_code is not None:
            return self._saml_status_code

        top_level_status_code = self.response_status.find('./samlp:StatusCode', NAMESPACES)
        if top_level_status_code.attrib['Value'].split(':')[-1].lower() != SUCCESS:
            second_level = top_level_status_code.find('./samlp:StatusCode', NAMESPACES)
            self._saml_status_code = second_level.attrib['Value']
            return self._saml_status_code

        self._saml_status_code = top_level_status_code.attrib['Value']
        return self._saml_status_code

    @property
    def status(self) -> str:
        if self._status is not None:
            return self._status
        status = self.saml_status_code.split(':')[-1]
        self._status = 'saml_' + CAMEL_TO_SNAKE_RE.sub('_', status).lower()
        return self._status

    @property
    def response_audience_restriction(self):
        if self._response_audience_restriction is not None:
            return self._response_audience_restriction

        self._response_audience_restriction = self.response.find('.//saml:AudienceRestriction', NAMESPACES)
        return self._response_audience_restriction

    @property
    def response_assertion(self):
        if self._response_assertion is not None:
            return self._response_assertion

        self._response_assertion = self.response.find('./saml:Assertion', NAMESPACES)
        return self._response_assertion

    @property
    def advice_assertion(self):
        if self._advice_assertion is not None:
            return self._advice_assertion

        self._advice_assertion = self.response_assertion.find('.//saml:Assertion', NAMESPACES)
        return self._advice_assertion

    @property
    def assertion_attribute_enc_key(self):
        if self._assertion_attribute_enc_key is not None:
            return self._assertion_attribute_enc_key

        self._assertion_attribute_enc_key = self.response_assertion.find('.//saml2:AttributeStatement//xenc:EncryptedKey', NAMESPACES)
        return self._assertion_attribute_enc_key

    @property
    def assertion_attribute_enc_data(self):
        if self._assertion_attribute_enc_data is not None:
            return self._assertion_attribute_enc_data

        self._assertion_attribute_enc_data = self.response_assertion.find('.//saml2:AttributeStatement//xenc:EncryptedData', NAMESPACES)
        return self._assertion_attribute_enc_data

    @property
    def issuer(self):
        if self._issuer is not None:
            return self._issuer

        self._issuer = self.root.find('./saml:Issuer', NAMESPACES)
        return self._issuer

    @property
    def response_issuer(self):
        if self._response_issuer is not None:
            return self._response_issuer

        self._response_issuer = self.response.find('./saml:Issuer', NAMESPACES)
        return self._response_issuer

    @property
    def assertion_issuer(self):
        if self._assertion_issuer is not None:
            return self._assertion_issuer

        self._assertion_issuer = self.response_assertion.find('./saml:Issuer', NAMESPACES)
        return self._assertion_issuer

    @property
    def advice_assertion_issuer(self):
        if self._advice_assertion_issuer is not None:
            return self._advice_assertion_issuer

        self._advice_assertion_issuer = self.advice_assertion.find('./saml:Issuer', NAMESPACES)
        return self._advice_assertion_issuer

    def raise_for_status(self) -> str:
        if self.status != 'saml_' + SUCCESS:
            raise UserNotAuthenticated("User authentication flow failed", oauth_error=self.status)

        return self.status

    def validate_in_response_to(self) -> List[ValidationError]:
        expected_entity_id = from_settings(self.provider.settings_dict, 'sp.entityId')
        response_conditions_aud = self.response_audience_restriction.find('.//saml:Audience', NAMESPACES)

        errors = []
        if expected_entity_id is None:
            errors.append(ValidationError('Could not read entity id from settings'))

        if response_conditions_aud is None:
            errors.append(ValidationError('Could not find response conditions audience in artifact response'))

        if response_conditions_aud.text != expected_entity_id:
            errors.append(ValidationError('Invalid audience in response Conditions. Expected {}, but was {}'.format(expected_entity_id, response_conditions_aud.text)))

        response_advice_encrypted_key_aud = self.assertion_attribute_enc_key
        if response_advice_encrypted_key_aud.attrib['Recipient'] != expected_entity_id:
            errors.append(ValidationError('Invalid audience in encrypted key. Expected {}, but was {}'.format(expected_entity_id, response_advice_encrypted_key_aud.attrib['Recipient'])))

        return errors

    def validate_issuer_texts(self) -> List[ValidationError]:
        expected_entity_id = self.provider.idp_metadata.entity_id
        errors = []
        if self.issuer.text != self.provider.idp_metadata.entity_id:
            errors.append(ValidationError('Invalid issuer in artifact response. Expected {}, but was {}'.format(expected_entity_id, self.issuer.text)))

        if self.response_issuer.text != self.provider.idp_metadata.entity_id:
            errors.append(ValidationError('Invalid issuer in artifact response_issuer. Expected {}, but was {}'.format(expected_entity_id, self.response_issuer.text)))

        if self.status == 'saml_' + SUCCESS:
            if self.assertion_issuer.text != self.provider.idp_metadata.entity_id:
                errors.append(ValidationError('Invalid issuer in artifact assertion_issuer. Expected {}, but was {}'.format(expected_entity_id, self.assertion_issuer.text)))

            # RD V.S. AD.
            if self.advice_assertion_issuer.text != self.provider.idp_metadata.entity_id:
                errors.append(ValidationError('Invalid issuer in artifact advice_assertion_issuer. Expected {}, but was {}'.format(expected_entity_id, self.advice_assertion_issuer.text)))

        return errors

    def validate(self) -> None:
        errors = []
        if self.status == 'saml_' + SUCCESS:
            errors += self.validate_in_response_to()

        errors += self.validate_issuer_texts()

        if len(errors) != 0:
            logging.error(errors)
            raise ValidationError('Audience verification errors.')

    def _decrypt_enc_key(self) -> bytes:
        aes_key = OneLogin_Saml2_Utils.decrypt_element(self.assertion_attribute_enc_key, self.provider.priv_key, debug=True)
        return aes_key

    def _decrypt_enc_data(self, aes_key: bytes) -> bytes:
        encrypted_ciphervalue = self.assertion_attribute_enc_data.find('.//xenc:CipherValue', {'xenc': 'http://www.w3.org/2001/04/xmlenc#'}).text
        b64decoded_data = base64.b64decode(encrypted_ciphervalue.encode())
        init_vector = b64decoded_data[:16]
        enc_data = b64decoded_data[16:]
        cipher = AES.new(aes_key, AES.MODE_CBC, iv=init_vector)
        plaintext = cipher.decrypt(enc_data)
        return remove_padding(plaintext)

    def get_bsn(self) -> Text:
        aes_key = self._decrypt_enc_key()
        bsn_element_raw = self._decrypt_enc_data(aes_key)
        bsn_element = etree.fromstring(bsn_element_raw.decode())
        return bsn_element.text
