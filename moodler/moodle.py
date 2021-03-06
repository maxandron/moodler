import logging
from typing import NamedTuple
from pathlib import Path
import csv
from collections import Counter, defaultdict

from moodler.assignment import get_assignments
from moodler.config import STUDENTS_TO_IGNORE, MOODLE_USERNAME, MOODLE_PASSWORD
from moodler.download import (
    download_file,
    download_submission,
    download_course_grades_report,
    DownloadException,
)
from moodler.moodle_connect import connect_to_server
from moodler.feedbacks import feedbacks
from moodler.students import get_students
from moodler.sections import core_course_get_contents, get_course_by_id

logger = logging.getLogger(__name__)


class StudentStatus(NamedTuple):
    """
    Consise submission status for one user.
    """

    user_name: str
    total_submissions: int
    last_submission: str


class SubmissionTuple(NamedTuple):
    """
    A helper named tuple for the status report function.
    Maps a submission name to a timestamp
    """

    name: str
    timestamp: int


def submissions_statistics(course_id, is_verbose=False, download_folder=None):
    """
    Returns a dictionary describing the status of ungraded exercises in the course.
    The dictionary looks like this:
        ```
        {
            'total_submissions': 10,
            'total_ungraded': 5,
            'total_resubmissions': 2,
            'exercises': {
                'assign1': {
                    'submissions': 2,
                    'ungraded': 2,
                    'resubmissions': 0}
                },
                ...
            }
        }
        ```

    If download_folder is set, downloads the ungraded exercises
    """
    logger.info("Showing ungraded submissions for course %s", course_id)
    total_submissions = 0
    total_ungraded = 0
    total_resubmissions = 0
    assignments_statistics = {}

    assignments = get_assignments(course_id)
    users_map = get_students(course_id)

    for assignment in assignments:
        current_assignment_submissions_amount = len(assignment.submissions)
        current_assignment_ungraded = assignment.ungraded()
        current_assignment_ungraded_amount = len(current_assignment_ungraded)

        current_assignment_resubmissions_amount = 0
        ungraded_ignored = []

        for submission in current_assignment_ungraded:
            if submission.resubmitted:
                current_assignment_resubmissions_amount += 1

            if submission.user_id in STUDENTS_TO_IGNORE.keys():
                ungraded_ignored.append(STUDENTS_TO_IGNORE[submission.user_id])

            if download_folder is not None:
                download_submission(
                    assignment.name,
                    users_map[submission.user_id],
                    submission,
                    download_folder,
                )

        total_submissions += current_assignment_submissions_amount
        total_ungraded += current_assignment_ungraded_amount
        total_resubmissions += current_assignment_resubmissions_amount

        # Print total stats about this assignment
        if is_verbose and len(ungraded_ignored) != 0:
            logger.info(
                "Ignored %s submissions for assignment '%s' (CMID %s, ID %s): %s",
                len(ungraded_ignored),
                assignment.name,
                assignment.cmid,
                assignment.uid,
                ungraded_ignored,
            )

        amount_ungraded_not_ignored = current_assignment_ungraded_amount - len(
            ungraded_ignored
        )
        if amount_ungraded_not_ignored != 0:
            logger.info(
                "Total ungraded for assignment [%s] (CMID %s, ID %s): %s/%s",
                assignment.name,
                assignment.cmid,
                assignment.uid,
                amount_ungraded_not_ignored,
                len(assignment.submissions),
            )

        assignments_statistics[assignment.name] = {
            "submissions": current_assignment_submissions_amount,
            "ungraded": amount_ungraded_not_ignored,
            "resubmissions": current_assignment_resubmissions_amount,
        }

    return {
        "total_submissions": total_submissions,
        "total_ungraded": total_ungraded,
        "total_resubmissions": total_resubmissions,
        "exercises": assignments_statistics,
    }


def export_feedbacks(course_id, folder: Path):
    """
    Exports the feedbacks of a course, in csv format, to a speicifed folder.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for feedback in feedbacks(course_id):
        if feedback.responses_count == 0:
            logger.info(f"Skipped empty feedback [{feedback.name}]")
            continue
        file_path = Path(folder) / Path(feedback.name).with_suffix(".csv")
        with file_path.open(mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(feedback.answers.keys())
            writer.writerows(zip(*feedback.answers.values()))


def export_submissions(course_id, download_folder):
    """
    Downloads all submissions from a given course
    """
    assignments = get_assignments(course_id)
    users_map = get_students(course_id)
    for assignment in assignments:
        for submission in assignment.submissions:
            download_submission(
                assignment.name,
                users_map[submission.user_id],
                submission,
                download_folder,
            )


def export_materials(course_id, folder):
    """
    Downloads all the materials from a course to a given folder
    """
    # Put assignments into a dict to find easily
    assigns = {assign.uid: assign for assign in get_assignments(course_id)}
    sections = core_course_get_contents(course_id)

    for section in sections:
        section_folder = Path(folder) / Path(section["name"])
        for module in section["modules"]:
            module_name = module["name"]
            module_type = module["modname"]

            if module_type not in ("resource", "folder", "assign", "url"):
                if module_type not in ["feedback", "forum"]:
                    logger.warning(
                        "Skipped export from unknown module '%s'", module_type
                    )
                continue

            # This is one of the known modules - create the section
            section_folder.mkdir(parents=True, exist_ok=True)

            if module_type in ("resource", "folder"):
                download_folder = section_folder
                # If its a folder, create a subfolder
                if module_type == "folder":
                    download_folder = download_folder / Path(module_name)
                    download_folder.mkdir(parents=True, exist_ok=True)

                for resource in module["contents"]:
                    download_file(resource["fileurl"], download_folder)
            elif module_type == "assign":
                # If module is an assignment - download attachments and description
                assign = assigns[module["instance"]]
                if len(assign.description) > 0:
                    description_file = section_folder / Path(
                        assign.name
                    ).with_suffix(".txt")
                    description_file.write_text(assign.description)
                for attachment in assign.attachments:
                    download_file(attachment, section_folder)
            elif module_type == "url":
                url_file = section_folder / Path(f"{module_name}_url.txt")
                # Assuming a url module can only have 1 url inside
                url_file.write_text(module["contents"][0]["fileurl"])


def export_grades(course_id, output_path, should_export_feedback=False):
    """
    Exports the complete grade file to the given folder in csv format
    """
    course = get_course_by_id(course_id)
    if not course:
        raise ValueError(f"Course with ID {course_id} not found.")

    session = connect_to_server(MOODLE_USERNAME, MOODLE_PASSWORD)

    try:
        grades_spreadsheet_file = download_course_grades_report(
            course.id, course.name, should_export_feedback, output_path, session
        )

        logger.info(
            "Downloaded the file '%s' as the grades exported spreadsheet",
            grades_spreadsheet_file,
        )
    except DownloadException:
        logger.exception("Failed downloading grade report")


def export_all(course_id, folder: Path):
    """
    Exports submissions, materials, and grades for the given course
    """
    export_grades(course_id, folder)
    export_materials(course_id, Path(folder) / "Materials")
    export_submissions(course_id, Path(folder) / "Submissions")
    export_feedbacks(course_id, Path(folder) / "Feedbacks")


def status_report(course_id):
    """
    Generates a short report of the students for a specific course.
    Returns a list of StudentStatus tuples.
    """
    assignments = get_assignments(course_id)
    users_map = get_students(course_id)

    submissions_by_user = Counter()
    last_submission_by_user = defaultdict(
        lambda: SubmissionTuple(name="Nothing", timestamp=0)
    )

    for assignment in assignments:
        for submission in assignment.submissions:
            user_name = users_map[submission.user_id]
            submissions_by_user[user_name] += 1

            if (
                last_submission_by_user[user_name].timestamp
                < submission.timestamp
            ):
                last_submission_by_user[user_name] = SubmissionTuple(
                    name=assignment.name, timestamp=submission.timestamp
                )

    student_statuses = []
    for user, submission_count in submissions_by_user.items():
        student_statuses.append(
            StudentStatus(
                user, submission_count, last_submission_by_user[user].name
            )
        )

    student_statuses = sorted(
        student_statuses,
        key=lambda student_status: student_status.total_submissions,
    )

    return student_statuses
