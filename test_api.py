import requests

# Any Python app can now use your service
with open('resume.pdf', 'rb') as f:
    response = requests.post('http://localhost:8000/recommend', 
        data={
            'job_title': 'Data Scientist',
            'department': 'Data',
            'job_description': 'We need ML expertise...'
        },
        files={'resume_file': f}
    )
    
recommendation = response.json()['recommendation']
print(recommendation)