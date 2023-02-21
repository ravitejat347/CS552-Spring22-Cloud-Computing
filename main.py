import cv2
import sys
from flask import Flask, render_template, Response, request, jsonify
import uuid
from camera import VideoCamera
from flask_basicauth import BasicAuth
from flask_socketio import SocketIO, emit
import time
from datetime import datetime
import threading
from threading import Lock
from polly import Polly
import json
from tempimage import TempImage
import boto3
from boto3.dynamodb.conditions import Key, Attr
import requests
#from flask_ngrok import run_with_ngrok
import uuid
from flask_cors import CORS,cross_origin
import base64
async_mode = None

conf = json.load(open("conf.json"))


video_camera = VideoCamera(flip=False) # creates a camera object, flip vertically
object_classifier = cv2.CascadeClassifier("models/haarcascade_frontalface_alt.xml") # an opencv classifier



app.config['SECRET_KEY'] = 'secret!'

CORS(app, resources={ r'/*': {'origins': '*'}}, supports_credentials=True)
socketio = SocketIO(app, async_mode=async_mode)
#run_with_ngrok(socketio) 
thread = None
thread_lock = Lock()
polly = Polly('Joanna')

s3client = boto3.resource('s3')
rekoclient=boto3.client('rekognition')
dynamodb = boto3.resource('dynamodb')
dbPiFaces = dynamodb.Table('PiFaces')
dbPiNgRok = dynamodb.Table('PiNgRok')
dbPiMessages = dynamodb.Table('PiMessages')
dbPiNotification= dynamodb.Table('PiNotification')
expiresIn=5*24*3600 #expires recorded voice after 5 days

basic_auth = BasicAuth(app)
last_epoch = 0
last_upload = 0
motionCounter = 0

def sortKey(e):
  return e['Similarity']

def scanMessages():
    response = dbPiMessages.scan()
    return response['Items']

def deleteMessage(id,createdOn):
    print("deleting message ",id)
    dbPiMessages.delete_item(
        Key={
            'id': id,
            'createdOn':createdOn
        }
    )
def deleteNotification(id,createdOn):
    print("deleting message ",id)
    dbPiNotification.delete_item(
        Key={
            'id': id,
            'createdOn':createdOn
        }
    )
def updateNotification(id,createdOn,faceId,faceName):
    dbPiNotification.update_item(
        Key ={
            'id': id,
            'createdOn':createdOn
        },
        UpdateExpression='SET faceId = :faceId, faceName=:faceName',
        ExpressionAttributeValues={
            ':faceName': faceName,
            ':faceId': faceId
        }
    )


def scanFaces():
    response = dbPiFaces.scan()
    data= response['Items']
    #signedUrl = s3client.meta.client.generate_presigned_url('get_object', Params = {'Bucket': conf["s3bucket_name"], 'Key': t.key}, ExpiresIn = expiresIn)
    for face in data:
       signedUrl = s3client.meta.client.generate_presigned_url('get_object', Params = {'Bucket': face["bucket"], 'Key': face["key"]}, ExpiresIn = expiresIn)
       face["url"]=signedUrl             
    return data

def deleteFace(faceId):

    faces=[]
    faces.append(faceId)
    rekoclient.delete_faces(CollectionId=conf["awsFaceCollection"],FaceIds=faces)

    dbPiFaces.delete_item(
        Key={
            'faceId': faceId
        }
    )



def search_face(data):
    print("searching face...", data["key"])
    try:
        matchedFace={}
        matched=False
        piFace=None
        response=rekoclient.search_faces_by_image(CollectionId=conf["awsFaceCollection"],
                                    Image={'S3Object':{'Bucket':conf["s3bucket_name"],'Name':data["key"]}},
                                    FaceMatchThreshold=80,
                                    MaxFaces=1)
        faceMatches=response['FaceMatches']
        faceMatches.sort(reverse=True,key=sortKey)

        if(len(faceMatches) > 0):
            matched=True
            matchedFace=faceMatches[0]
            piFace=dbPiFaces.get_item(
                Key={
                    'faceId':matchedFace['Face']['FaceId']
                }
            )['Item']
            #piFace = response['Item']


        return matched,piFace
    except:
       print ("Error in AWS Reko: ", sys.exc_info()[0])                          

def check_for_objects():
    global last_epoch
    global last_upload
    global motionCounter
    while True:
        try:
            _, found_obj,frame = video_camera.get_object(object_classifier)
            if found_obj and (time.time() - last_epoch) > conf["min_motion_window"]:
                
                motionCounter += 1
                print("$$$ object found with motion counter ", motionCounter)
                ##print("min_motion_frames",conf["min_motion_frames"])
                print("last upload ",(time.time() - last_upload), " seconds ago")
                ##print("min upload interval ",conf["upload_interval"])
                last_epoch = time.time()
                print("motionCounter", motionCounter)
                print("(time.time() - last_upload)",(time.time() - last_upload))
                if motionCounter >= conf["min_motion_frames"] and (time.time() - last_upload) > conf["upload_interval"] :
                    print("$$$ Upload image to S3 Bucket - ",conf["s3bucket_name"])
                    last_upload = time.time()
                    motionCounter = 0
                    t = TempImage()
                    cv2.imwrite(t.path, frame)
                    #print(conf["s3bucket_name"])
                    #print("$$$ 111")
                    #print(t.path)
                    print("$$$ Image ID - ",t.key)
                    s3client.meta.client.upload_file(t.path, conf["s3bucket_name"], t.key )
                    print("$$$ Uploaded")
                    signedUrl = s3client.meta.client.generate_presigned_url('get_object', Params = {'Bucket': conf["s3bucket_name"], 'Key': t.key}, ExpiresIn = expiresIn)
                    ##print("$$$ 333")
                    faceId=None
                    faceName="Visitor"
                    matched = False
                    print("$$$ Search for Visitor in data base")
                    if conf["use_rekognition"]== True:
                        time.sleep(1)
                        rekodata ={}
                        rekodata["key"]=t.key
                        matched,piFace = search_face(rekodata)
                        if matched == True:
                            faceId = piFace['faceId']
                            faceName = piFace['faceName']

                    ##print("$$$ Visitor found ")
                    slack_data = {
                        'attachments': [
                            {
                                'color': "#36a64f",
                                'pretext': faceName+ " at the front door",
                                'title': "Smart Camera",
                                'title_link': "https://smart-camera-552.com",
                                'image_url': signedUrl
                        
                            }
                        ]
                    }
                    print("$$$ Visitor face image is sent to Slack ")
                    requests.post(conf["slack_incoming_webhook"], data=json.dumps(slack_data),headers={'Content-Type': 'application/json'})
                    print("$$$ Slack Notofied")
                    ##print(signedUrl)
                    persistNotification({
                        "id": str(uuid.uuid4()),
                        "createdOn": datetime.now().isoformat(),
                        "bucket": conf["s3bucket_name"],
                        "signedUrl":signedUrl,
                        "key":t.key,
                        "faceId": faceId,
                        "faceName":faceName
                    })
                    ##print("$$$ 777")
                    t.cleanup()
                    print("prompt visitor to leave voice message")
                    # prompt visitor to leave voice message
                    polly.speak("Hello "+faceName+"....  Please leave your brief message after the beep....")
                    print("Hello //FaceName//....  Please leave your brief message after the beep....")
                    print("$$$ Audio mesage delivered ")
                    print("Recording Visitor Voice")
                    recordGuestVoice()
                    print("Visitor voice recorded")
                    

                else:
                    print("Visitor captured")
                    print("$$$ dont upload")
                
        except:
            print ("Error sending email: ", sys.exc_info()[0])

def persistMessage(Item):
    dbPiMessages.put_item(
        Item=Item
    )   
def persistNotification(Item):
    dbPiNotification.put_item(
        Item=Item
    ) 



if __name__ == '__main__':
    t = threading.Thread(target=check_for_objects, args=())
    t.daemon = True
    t.start()
    #app.run(host='0.0.0.0', debug=False)
    socketio.run(app,host='0.0.0.0', debug=False)
    #socketio.run(app)
