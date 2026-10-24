from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    """Registro de resultado: imutavel, sem revalidacao em copias (as listas chegam a dezenas de milhares)."""

    model_config = ConfigDict(frozen=True)
