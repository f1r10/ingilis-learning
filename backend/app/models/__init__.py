"""Import all models so `Base.metadata` is complete for Alembic autogenerate."""
from app.core.database import Base  # noqa: F401  (import first: defines Base)
from app.models import (
    activity,  # noqa: F401
    assessment,  # noqa: F401
    base,  # noqa: F401
    content,  # noqa: F401
    identity,  # noqa: F401
    ops,  # noqa: F401
    system,  # noqa: F401
)
from app.models.activity import (  # noqa: F401
    ActivityEvent,
    Favorite,
    Notification,
    QuestionReport,
)
from app.models.assessment import (  # noqa: F401
    AttemptAnswer,
    Catalog,
    CatalogItem,
    Exam,
    ExamAssignment,
    ExamAttempt,
    ExamItem,
    ExamSection,
    ManualReview,
    TeacherFeedback,
)
from app.models.content import (  # noqa: F401
    ImportItem,
    ImportJob,
    Listening,
    ListeningQuestionSet,
    MediaAsset,
    Question,
    QuestionTag,
    QuestionTopic,
    QuestionVersion,
    Reading,
    ReadingQuestionSet,
    SourceFile,
    Tag,
    Topic,
    VocabularyEntry,
    VocabularyExample,
    VocabularyTranslation,
)
from app.models.identity import (  # noqa: F401
    AdminRecoveryCode,
    AdminUser,
    Group,
    GroupMembership,
    Student,
    StudentAccessKey,
    StudentNote,
    StudentSession,
)
from app.models.ops import AuditLog, Backup, ExportJob  # noqa: F401

# Re-export the most-used classes for convenient imports.
from app.models.system import Language, SystemSetting  # noqa: F401

__all__ = [
    "Base",
    "SystemSetting",
    "Language",
    "AdminUser",
    "AdminRecoveryCode",
    "Student",
    "StudentAccessKey",
    "StudentSession",
    "Group",
    "GroupMembership",
    "StudentNote",
    "Topic",
    "Tag",
    "SourceFile",
    "ImportJob",
    "ImportItem",
    "MediaAsset",
    "Question",
    "QuestionVersion",
    "QuestionTopic",
    "QuestionTag",
    "VocabularyEntry",
    "VocabularyTranslation",
    "VocabularyExample",
    "Reading",
    "ReadingQuestionSet",
    "Listening",
    "ListeningQuestionSet",
    "Catalog",
    "CatalogItem",
    "Exam",
    "ExamSection",
    "ExamItem",
    "ExamAssignment",
    "ExamAttempt",
    "AttemptAnswer",
    "ManualReview",
    "TeacherFeedback",
    "ActivityEvent",
    "Favorite",
    "QuestionReport",
    "Notification",
    "ExportJob",
    "Backup",
    "AuditLog",
]
