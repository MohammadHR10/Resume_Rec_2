import requests
import json

# Test the enhanced API with structured JSON responses

def test_single_resume():
    """Test single resume analysis with JSON validation"""
    
    # Replace with an actual PDF file path
    pdf_file_path = "resume.pdf"  # You'll need to have a PDF file
    
    try:
        with open(pdf_file_path, 'rb') as f:
            response = requests.post('http://localhost:8000/recommend', 
                data={
                    'job_title': 'Data Scientist',
                    'department': 'Data',
                    'job_description': '''We are looking for a skilled Data Scientist with experience in:
                    - Python and R programming
                    - Machine Learning algorithms
                    - Statistical analysis
                    - Data visualization tools
                    - SQL and database management
                    - Experience with cloud platforms (AWS, GCP)
                    '''
                },
                files={'resume_file': f}
            )
        
        print("Status Code:", response.status_code)
        print("Response Headers:", dict(response.headers))
        
        if response.status_code == 200:
            result = response.json()
            print("\n=== STRUCTURED EVALUATION ===")
            print(f"Candidate: {result['candidate_name']}")
            print(f"Overall Score: {result['overall_score']}/10")
            print(f"Recommendation: {result['recommendation']}")
            print(f"\nKey Strengths:")
            for strength in result['key_strengths']:
                print(f"  • {strength}")
            print(f"\nPotential Concerns:")
            for concern in result['potential_concerns']:
                print(f"  • {concern}")
            print(f"\nSkills Match: {result['skills_match']}")
            print(f"Experience Relevance: {result['experience_relevance']}")
            
        else:
            print("Error Response:")
            print(json.dumps(response.json(), indent=2))
            
    except FileNotFoundError:
        print(f"Error: Could not find resume file at {pdf_file_path}")
        print("Please update the pdf_file_path variable with a valid PDF file path")
    except Exception as e:
        print(f"Error: {e}")

def test_health_check():
    """Test API health"""
    response = requests.get('http://localhost:8000/health')
    print("Health Check:", response.json())

def test_batch_analysis():
    """Test batch resume analysis (if you have multiple PDFs)"""
    pdf_files = ["resume1.pdf", "resume2.pdf"]  # Update with actual file paths
    
    try:
        files = []
        for pdf_path in pdf_files:
            files.append(('resume_files', (pdf_path, open(pdf_path, 'rb'), 'application/pdf')))
        
        response = requests.post('http://localhost:8000/batch_recommend',
            data={
                'job_title': 'Software Engineer',
                'department': 'Engineering',
                'job_description': 'Looking for full-stack developers with React and Python experience'
            },
            files=files
        )
        
        # Close file handles
        for _, (_, file_handle, _) in files:
            file_handle.close()
        
        if response.status_code == 200:
            result = response.json()
            print(f"\n=== BATCH ANALYSIS RESULTS ===")
            print(f"Total Resumes: {result['total_resumes']}")
            print(f"Successful Evaluations: {result['successful_evaluations']}")
            
            for i, resume_result in enumerate(result['results']):
                print(f"\n--- Resume {i+1}: {resume_result['filename']} ---")
                if resume_result['success']:
                    eval_data = resume_result['evaluation']
                    print(f"Score: {eval_data['overall_score']}/10")
                    print(f"Recommendation: {eval_data['recommendation']}")
                else:
                    print(f"Error: {resume_result['error']}")
        else:
            print("Batch Error:", response.json())
            
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Please update pdf_files list with valid PDF file paths")

if __name__ == "__main__":
    print("Testing Resume Recommender API with JSON Validation")
    print("=" * 50)
    
    # Test health first
    test_health_check()
    print()
    
    # Test single resume analysis
    test_single_resume()
    
    # Uncomment to test batch analysis
    # test_batch_analysis()