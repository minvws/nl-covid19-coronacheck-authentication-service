class SubjectIdentifierFactory(object):
    ...

class HashBasedSubjectIdentifierFactory(SubjectIdentifierFactory):
    def __init__(self, hash_salt: str) -> None: ...