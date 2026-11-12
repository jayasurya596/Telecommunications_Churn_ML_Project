"""AWS Lambda adapter for the India-region FastAPI service."""

from mangum import Mangum

from .api import app

handler = Mangum(app, lifespan="off")
