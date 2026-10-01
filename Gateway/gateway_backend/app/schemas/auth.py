from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=6)


class SigninRequest(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    form_filled: bool

    class Config:
        from_attributes = True


class AuthResponse(BaseModel):
    token: str
    user: UserResponse


class ServiceTokenRequest(BaseModel):
    client_id: str
    client_secret: str


class ServiceTokenResponse(BaseModel):
    token: str
    scopes: list[str]
    expires_in: int             # seconds until the service token expires


class CredentialsSubmit(BaseModel):
    broker_name: str            # "shoonya"
    label: str | None = None    # display name; auto-generated when omitted
    credentials: dict           # broker-specific fields; encrypted before storage


class ConfiguredBrokerResponse(BaseModel):
    id: int
    broker_name: str
    label: str
    is_active: bool             # the account used by /api/connect and the trading engine
    fields_present: list[str]   # which credential keys are stored (names only, never values)


class CredentialRevealRequest(BaseModel):
    password: str               # app password, re-verified before decrypting secrets


class CredentialRevealResponse(BaseModel):
    id: int
    broker_name: str
    label: str
    credentials: dict           # decrypted plaintext values — only returned after password check


class CredentialTestRequest(BaseModel):
    """Test broker credentials currently open in the form (not yet saved, or saved)."""
    broker_name: str
    credentials: dict


class StoredCredentialTestRequest(BaseModel):
    """Test a saved account's stored credentials, gated by the app password."""
    password: str


class CredentialTestResponse(BaseModel):
    success: bool
    message: str
