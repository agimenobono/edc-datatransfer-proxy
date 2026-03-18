from pydantic import BaseModel, Extra, validator


class DownloadRequest(BaseModel):
    endpoint: str
    authorization: str

    class Config:
        extra = Extra.forbid

    @validator("endpoint")
    def reject_query_and_fragment(cls, value: str) -> str:
        if "?" in value or "#" in value:
            raise ValueError("endpoint must not contain query string or fragment")
        return value
