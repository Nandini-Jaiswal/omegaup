# mypy: ignore-errors

import json
import time
import argparse
from getpass import getpass
import urllib.parse
import requests
from tqdm import tqdm
from openai import OpenAI


KEY = None
USERNAME = None
PASSWORD = None
LANGUAGE = None
COURSE_ALIAS = None
ASSIGNMENT_ALIAS = None

BASE_URL = "https://omegaup.com"
COOKIES = None
CLIENT = None


def get_login_endpoint(username, password):
    """endpoint for logging in"""
    return f"api/user/login?usernameOrEmail={username}&password={password}"


def get_problem_details_endpoint(problem_alias):
    """endpoint for getting problem details"""
    return f"api/problem/details?problem_alias={problem_alias}"


def get_problem_solution_endpoint(problem_alias):
    """endpoint for getting problem solution"""
    return f"api/problem/solution?problem_alias={problem_alias}"


def get_runs_endpoint(run_alias):
    """endpoint for getting runs"""
    return f"api/run/details?run_alias={run_alias}"


def get_runs_submission_feedback_endpoint(run_alias):
    """endpoint for getting runs submission feedback"""
    return f"api/run/getSubmissionFeedback?run_alias={run_alias}"


def set_submission_feedback_endpoint(
    run_alias,
    course_alias,
    assignment_alias,
    feedback,
    line_number,
    submission_feedback_id,
):
    """endpoint for setting submission feedback"""
    return (
        f"api/submission/setFeedback?"
        f"guid={run_alias}&"
        f"course_alias={course_alias}&"
        f"assignment_alias={assignment_alias}&"
        f"feedback={feedback}&"
        f"range_bytes_start={line_number}&"
        f"submission_feedback_id={submission_feedback_id}"
    )


def set_submission_feedback_list_endpoint(
    run_alias, course_alias, assignment_alias, feedback_list
):
    """endpoint for setting submission feedback list"""
    return (
        f"api/submission/setFeedbackList?"
        f"guid={run_alias}&"
        f"course_alias={course_alias}&"
        f"assignment_alias={assignment_alias}&"
        f"feedback_list={feedback_list}"
    )


def get_runs_from_course_endpoint(
    course_alias, assignment_alias, rowcount=None, offset=None
):
    endpoint = (
        f"/api/course/runs?"
        f"course_alias={course_alias}&"
        f"assignment_alias={assignment_alias}"
    )

    if rowcount is not None:
        endpoint += f"&rowcount={rowcount}"
    if offset is not None:
        endpoint += f"&offset={offset}"
    return endpoint


def get_contents_from_url(get_endpoint_fn, args=None):
    """hit the endpoint with GET request"""
    global COOKIES
    global BASE_URL

    if args is None:
        args = {}
    endpoint = get_endpoint_fn(**args)
    url = f"{BASE_URL}/{endpoint}"

    if get_endpoint_fn == get_login_endpoint:
        COOKIES = None

    try:
        if COOKIES is None:
            response = requests.get(url)
            response.raise_for_status()
            COOKIES = response.cookies
        else:
            response = requests.get(url, COOKIES)
            response.raise_for_status()
        data = response.json()
        return data
    except requests.exceptions.RequestException as e:
        raise
    except json.JSONDecodeError as e:
        raise


def extract_show_run_ids():
    """
    Extracts show-run IDs and usernames from the course.

    Returns:
        list: List of all the latest (at most 30 days old) run IDs and the
        usernames from the course
    """
    runs = get_contents_from_url(
        get_runs_from_course_endpoint,
        {"course_alias": COURSE_ALIAS, "assignment_alias": ASSIGNMENT_ALIAS},
    )["runs"]

    current_time = int(time.time())
    a_month_ago = current_time - (30 * 24 * 60 * 60)

    run_ids_and_usernames = [
        (item["guid"], item["username"])
        for item in runs
        if item["time"] >= a_month_ago
    ]

    return run_ids_and_usernames


def extract_feedback_thread(run_alias):
    """
    Extracts feedback thread from a run.

    Returns:
    list: List of feedback threads
    """
    submission_feedback_requests = get_contents_from_url(
        get_runs_submission_feedback_endpoint, {"run_alias": run_alias}
    )

    conversations = []
    for feedback_request in submission_feedback_requests:
        conversation = []
        conversation.append({
            "line_number": feedback_request["range_bytes_start"]
        })
        conversation.append({
            "feedback_id": feedback_request["submission_feedback_id"]
        })
        conversation.append({
            feedback_request["author"]: feedback_request["feedback"]
        })

        if "feedback_thread" in feedback_request:
            for feedback in feedback_request["feedback_thread"]:
                conversation.append({feedback["author"]: feedback["text"]})

        conversations.append(conversation)

    return conversations


def conjure_query(
    problem_statement,
    solution_statement,
    source_code,
    feedback,
    user_name,
    line_number,
    is_conversation,
):
    """
    Conjures a string that can be used as a prompt to the LLM.

    Returns:
    string: Conjured query
    """
    conjured_query = ""
    if is_conversation:
        conjured_query = (
            f"The problem statement is: {problem_statement}\n"
            f"The solution is: {solution_statement}\n"
            f"The Source code is: {source_code}\n\n"
            f"Note the line number: {line_number}\n"
            f"Remember that you are {USERNAME} "
            f"and the student is {user_name}\n"
            f"The conversation is: {str(feedback)}"
            f"Please just return text that continues the conversation, "
            f"return no json in this case."
        )
    else:
        conjured_query = (
            f"The problem statement is: {problem_statement}\n"
            f"The solution is: {solution_statement}\n"
            f"The Source code is: {source_code}\n\n"
            f"Please give feedback on the source code "
            f"using the above chain of thoughts.\n"
            f"Just return the json, don't use markdown to include ```.\n"
        )
    return conjured_query


def get_prompt(query_content):
    with open("./teaching_assistant_prompt.txt", "r") as file:
        prompt = file.read()
    return prompt.format(LANGUAGE=LANGUAGE, query_content=query_content)


def query_LLM(query_content, is_initial_feedback=True, temperature=0):
    """
    Queries the LLM and returns the response.

    Returns:
    string: Response from the LLM
    """

    prompt = get_prompt(query_content=query_content)

    response = CLIENT.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=500,
    )
    response_text = response.choices[0].message.content

    if not is_initial_feedback and len(response_text) > 1000:
        concise_request = (
            f"Can you make the following response concise and try to limit it "
            f"within 1000 characters? {response_text}"
        )

        response = CLIENT.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": concise_request}],
            temperature=temperature,
            max_tokens=500,
        )
        response_text = response.choices[0].message.content

    return response_text


def process_initial_feedback(
    TA_feedback, show_run_id, course_alias, assignment_alias
):

    """
    Gives initial feedback when a students asks for help to correct a
    submission

    Returns:
    None
    """
    for line, feedback in TA_feedback.items():
        if line == "general advices":
            continue
        feedback_list = (
            '[{"lineNumber": ' + str(line) + ', "feedback": "'
            + feedback[:1000] + '"}]'
        )
        get_contents_from_url(
            set_submission_feedback_list_endpoint,
            {
                "run_alias": show_run_id,
                "course_alias": course_alias,
                "assignment_alias": assignment_alias,
                "feedback_list": feedback_list,
            },
        )


def process_feedbacks():
    """
    Processes feedback requests from students using LLM oracle.

    Returns:
    None
    """
    get_contents_from_url(
        get_login_endpoint, {"username": USERNAME, "password": PASSWORD}
    )
    run_ids_and_usernames = extract_show_run_ids()
    for run_id, user_name in tqdm(run_ids_and_usernames):
        course_alias = COURSE_ALIAS
        assignment_alias = ASSIGNMENT_ALIAS
        run_details = get_contents_from_url(
            get_runs_endpoint, {"run_alias": run_id}
        )

        problem_alias = run_details["alias"]

        source_content = run_details["source"]

        problem_content = get_contents_from_url(
            get_problem_details_endpoint, {"problem_alias": problem_alias}
        )["statement"]["markdown"]
        problem_solution = get_contents_from_url(
            get_problem_solution_endpoint, {"problem_alias": problem_alias}
        )["solution"]["markdown"]

        feedbacks = extract_feedback_thread(run_id)

        if len(feedbacks) == 0:
            continue

        is_initial_feedback = len(feedbacks) == 1

        for feedback in feedbacks:
            if user_name not in feedback[-1]:
                continue
            line_number = feedback[0]["line_number"]
            feedback_id = feedback[1]["feedback_id"]
            conjured_query = conjure_query(
                problem_content,
                problem_solution,
                source_content,
                feedback[2:],
                user_name,
                line_number,
                line_number is not None,
            )
            if line_number is not None:
                oracle_feedback = query_LLM(
                    conjured_query, is_initial_feedback=False
                )
                get_contents_from_url(
                    set_submission_feedback_endpoint,
                    {
                        "run_alias": run_id,
                        "course_alias": course_alias,
                        "assignment_alias": assignment_alias,
                        "feedback": urllib.parse.quote(oracle_feedback[:1000]),
                        "line_number": line_number,
                        "submission_feedback_id": feedback_id,
                    },
                )
            else:
                if is_initial_feedback:
                    oracle_feedback = query_LLM(
                        conjured_query,
                    )
                    oracle_feedback = json.loads(oracle_feedback)
                    process_initial_feedback(
                        oracle_feedback, run_id, course_alias, assignment_alias
                    )


def main():
    global USERNAME, PASSWORD, COURSE_ALIAS, ASSIGNMENT_ALIAS
    global LANGUAGE, KEY, CLIENT
    parser = argparse.ArgumentParser(
        description="Process feedbacks from students"
    )
    parser.add_argument("--username", type=str, help="Your username")
    parser.add_argument("--password", type=str, help="Your password")
    parser.add_argument(
        "--course_alias",
        type=str,
        help="Course alias to process feedbacks for"
    )
    parser.add_argument(
        "--assignment_alias",
        type=str,
        help="Assignment alias to process feedbacks for"
    )
    parser.add_argument(
        "--language", type=str, help="Language to use for feedbacks"
    )
    parser.add_argument("--key", type=str, help="API key for OpenAI")
    args = parser.parse_args()

    USERNAME = args.username or input("Enter your username: ")
    PASSWORD = args.password or getpass("Enter your password: ")
    COURSE_ALIAS = args.course_alias or input("Enter the course alias: ")
    ASSIGNMENT_ALIAS = (
        args.assignment_alias or input("Enter the assignment alias: ")
    )
    LANGUAGE = args.language or input("Enter the language: ")
    KEY = args.key or getpass("Enter your OpenAI API key: ")

    CLIENT = OpenAI(api_key=KEY)

    process_feedbacks()


if __name__ == "__main__":
    main()