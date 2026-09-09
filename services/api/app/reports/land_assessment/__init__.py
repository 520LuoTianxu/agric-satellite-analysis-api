"""选地体检 (land assessment) plain-language PDF report."""

__all__ = ["generate_assessment_pdf"]


def __getattr__(name: str):
    if name == "generate_assessment_pdf":
        from app.reports.land_assessment.service import generate_assessment_pdf

        return generate_assessment_pdf
    raise AttributeError(name)
