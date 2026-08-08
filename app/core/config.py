from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from .env file
    Controls API, server, and MongoDB configuration
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    
    # API Configuration
    API_TITLE: str = "Power Trading API"
    API_VERSION: str = "v1"
    DEBUG: bool = False
    
    # MongoDB Configuration
    MONGODB_URL: str = "mongodb://localhost:27017"
    DATABASE_NAME: str = "power_trading"
    
    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # External public calendar APIs
    PUBLIC_HOLIDAYS_API_BASE_URL: str = "https://date.nager.at/api/v3"

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug_value(cls, value):
        """
        Accept common deployment values because DEBUG is a generic env var
        that may already exist in the host shell.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on", "debug", "dev", "development"}:
                return True
            if normalized in {"false", "0", "no", "n", "off", "release", "prod", "production"}:
                return False
        return value
    
settings = Settings()
