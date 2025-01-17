import os
import numpy as np
import quaternion as quat
import open3d as o3d
from copy import deepcopy as dcp
from geometry_msgs.msg import PoseStamped


script_dir = os.path.dirname(os.path.realpath(__file__))
# load hand-calibrated transformation matrices
transform_base_to_cam_hand_calibrated = np.load(os.path.join(script_dir, 'transformation_matrices', 'transform_base_to_cam_fine_tuned.npy'))
transform_cam_to_base_hand_calibrated = np.load(os.path.join(script_dir, 'transformation_matrices', 'transform_cam_to_base_fine_tuned.npy'))
transform_base_to_reference_grasp = np.load(os.path.join(script_dir, 'transformation_matrices', 'transform_base_to_reference_grasp.npy'))

# load and create a bounding box
workspace_bounding_box_array = np.load(os.path.join(script_dir, 'transformation_matrices', 'workspace_bounding_box_array_in_base.npy'))
workspace_bounding_box_array = o3d.utility.Vector3dVector(workspace_bounding_box_array.astype('float64'))
workspace_bounding_box = o3d.geometry.OrientedBoundingBox.create_from_points(points=workspace_bounding_box_array)
workspace_bounding_box.color = (0, 1, 0)

# The x, y, z axis will be rendered as red, green, and blue arrows respectively.
# Robot frame
robot_frame = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.2)
# Camera frame, transformed into robot frame
cam_frame = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.2)
cam_frame.transform(transform_cam_to_base_hand_calibrated)

# default registration parameters unit: meter
VOXEL_SIZE = 0.005
RADIUS_NORMAL = 0.005
RADIUS_FEATURE = 0.03
GLOBAL_DISTANCE_THRESHOLD = 0.2
ICP_REFINE_DISTANCE_THRESHOLD = 0.002


def preprocess_point_cloud(pcd, voxel_size=VOXEL_SIZE, radius_normal=RADIUS_NORMAL, radius_feature=RADIUS_FEATURE):
    pcd_down = pcd.voxel_down_sample(voxel_size)
    pcd_down.estimate_normals(search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=radius_normal, max_nn=100))
    pcd_fpfh = o3d.registration.compute_fpfh_feature(
        input=pcd_down,
        search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=radius_feature, max_nn=200))
    return pcd_down, pcd_fpfh


def execute_fast_global_registration(source_down, target_down, source_fpfh, target_fpfh,
                                     distance_threshold=GLOBAL_DISTANCE_THRESHOLD):
    print("[INFO] Fast global registration with fpfh features")
    result = o3d.registration.registration_fast_based_on_feature_matching(
        source=source_down,
        target=target_down,
        source_feature=source_fpfh,
        target_feature=target_fpfh,
        option=o3d.registration.FastGlobalRegistrationOption(maximum_correspondence_distance=distance_threshold)
    )
    return result


def refine_registration(source, target, previous_transformation,
                        distance_threshold=ICP_REFINE_DISTANCE_THRESHOLD):
    print("[INFO] Point-to-plane ICP registration")
    result = o3d.registration.registration_icp(
        source, target, distance_threshold, previous_transformation,
        o3d.registration.TransformationEstimationPointToPlane())
    return result


# load, preprocess target point cloud (one with a reference grasp)
target = o3d.io.read_point_cloud(os.path.join(script_dir, 'reference_grasp', 'cropped_pcd_reference_in_world_frame.ply'))
# into robot frame
target.transform(transform_cam_to_base_hand_calibrated)
target.paint_uniform_color([0.6, 0.6, 0.6])
# compute fpfh
target_down, target_fpfh = preprocess_point_cloud(target)
# a transformation to move the source pcd away from the target
init_transformation_for_global_registration = transform_base_to_cam_hand_calibrated.copy()

# PoseStamped msg
pose_msg = PoseStamped()


def get_target_grasp_pose(source_pcd):
    # copy the source pcd and transform into robot frame
    source_pcd_original = dcp(source_pcd)
    source_pcd_original.paint_uniform_color([0, 0, 1])
    source_pcd_original.transform(transform_cam_to_base_hand_calibrated)
    # copy one to be processed, and crop
    source_pcd_to_process = dcp(source_pcd_original)
    source_pcd_to_process.crop(workspace_bounding_box)

    # pre-processing for registration
    # transform the source pcd to an arbitrary pose far away from the target
    source_pcd_to_process.transform(init_transformation_for_global_registration)
    # compute fpfh
    source_down, source_fpfh = preprocess_point_cloud(source_pcd_to_process)
    # global
    result_fast = execute_fast_global_registration(source_down, target_down, source_fpfh, target_fpfh)
    # icp refinement
    result_refine = refine_registration(source_down, target_down, result_fast.transformation)
    # final transformation should includes the initial transformation
    transform_source_to_target = np.matmul(result_refine.transformation, init_transformation_for_global_registration)

    # visualizing result
    new_grasp_frame = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.2)
    transform_target_to_source = np.linalg.inv(transform_source_to_target)
    transform_target_grasp = np.matmul(transform_target_to_source, transform_base_to_reference_grasp)
    new_grasp_frame.transform(transform_target_grasp)
    print('[INFO] Visualizing the found grasping pose')
    o3d.visualization.draw_geometries([robot_frame, new_grasp_frame, source_pcd_original, target],
                                      window_name='Grasping pose proposal', width=1200, height=960)

    # convert transformation matrix to coordinate and quaternion
    rotation_quat = quat.from_rotation_matrix(transform_target_grasp[:-1, :-1])  # this is already normalized
    rotation_quat = quat.as_float_array(rotation_quat).tolist()  # wxyz
    translate_matrix = transform_target_grasp[:-1, -1].tolist()  # xyz
    # construct and return a PoseStamped msg
    pose = dcp(pose_msg)
    pose.pose.position.x = translate_matrix[0]
    pose.pose.position.y = translate_matrix[1]
    pose.pose.position.z = translate_matrix[2]
    pose.pose.orientation.w = rotation_quat[0]
    pose.pose.orientation.x = rotation_quat[1]
    pose.pose.orientation.y = rotation_quat[2]
    pose.pose.orientation.z = rotation_quat[3]
    return pose


# for i in ['0', '1', '2', '3', '4']:
#     source_original = o3d.io.read_point_cloud(os.path.join(script_dir, '..', '..', '..', 'camera_test', 'objects', 'pcl_part', 'part_xyz_'+i+'.ply'))
#     _ = get_target_grasp_pose(source_original)
